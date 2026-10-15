"""Implementation-loop logic: aggregation of review findings and progress / stall detection.

Everything here is pure (no I/O except through callables passed in), so the rules that decide
"revise", "pass", "stalled" or "exhausted" are unit-tested directly.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Callable

SEVERITY_RANK = {"blocking": 0, "major": 1, "minor": 2}

# Rules whose presence means the previous patch is wrong (a regression or an integrity violation) and
# should be discarded rather than built upon. Unresolved review probes and repeated solutions are not on
# this list: they mean the patch is incomplete or the feedback was ignored, so the next revision builds on
# it (and the identical patch is excluded by fingerprint).
RESTART_RULES = {"repro-failing", "suite-failing", "tests-modified", "out-of-scope", "secret"}

# Runs one doctest-format probe file inside the sandbox: `python -c PROBE_RUNNER <file>`.
PROBE_RUNNER = (
    "import doctest, sys\n"
    "src = open(sys.argv[1], encoding='utf-8').read()\n"
    "t = doctest.DocTestParser().get_doctest(src, {}, 'probe', 'probe', 0)\n"
    "r = doctest.DocTestRunner(optionflags=doctest.NORMALIZE_WHITESPACE | doctest.ELLIPSIS)\n"
    "out = []\n"
    "res = r.run(t, out=out.append)\n"
    "print(''.join(out) or 'ok')\n"
    "sys.exit(1 if res.failed else 0)\n"
)


def fingerprint(contents: dict[str, str]) -> str:
    """Identity of a solution: hash of the final contents of the planned target files."""
    h = hashlib.sha256()
    for name in sorted(contents):
        h.update(name.encode() + b"\0" + contents[name].replace("\r\n", "\n").encode() + b"\0")
    return h.hexdigest()[:16]


def probe_id(source: str) -> str:
    return "P-" + hashlib.sha1(source.strip().encode()).hexdigest()[:8]


def parse_unified_diff(text: str) -> list[dict]:
    """Parse `git diff -U0` output into hunks with old/new line numbers."""
    hunks, file = [], None
    old_no = new_no = 0
    for line in text.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            old_no, new_no = int(m.group(1)), int(m.group(2))
            hunks.append({"file": file, "removed": [], "added": []})
        elif hunks and line.startswith("-") and not line.startswith("---"):
            hunks[-1]["removed"].append((old_no, line[1:]))
            old_no += 1
        elif hunks and line.startswith("+"):
            hunks[-1]["added"].append((new_no, line[1:]))
            new_no += 1
    return hunks


# ------------------------------------------------------------------ aggregation
def _norm(f: dict, reviewer: str) -> dict:
    sev = f.get("severity", "major")
    sev = sev if sev in SEVERITY_RANK else "major"
    return {"severity": sev, "rule": f.get("rule") or "review", "title": str(f.get("title", "")).strip()[:200],
            "detail": str(f.get("detail", ""))[:2000], "file": f.get("file"), "line": f.get("line"),
            "function": f.get("function"), "evidence": str(f.get("evidence", ""))[:3000],
            "suggestion": str(f.get("suggestion", ""))[:1000], "probe": f.get("probe") or None,
            "reviewer": reviewer, "confirmed": None}


def test_findings(tests: dict) -> list[dict]:
    """Deterministic test results are findings too, with the 'tests' source."""
    out = []
    if not tests.get("repro_passed"):
        out.append({"severity": "blocking", "rule": "repro-failing", "title": "Reproduction test still fails",
                    "detail": tests.get("repro_summary", ""), "evidence": tests.get("repro_output", "")})
    if not tests.get("suite_passed"):
        out.append({"severity": "blocking", "rule": "suite-failing", "title": "Regression suite fails",
                    "detail": tests.get("suite_summary", ""), "evidence": tests.get("suite_output", "")})
    for p in tests.get("probes", []):
        if not p["passed"]:
            out.append({"severity": "blocking", "rule": "probe-failing", "probe": p["source"],
                        "title": f"Previously reported defect still present ({p['id']})", "evidence": p["output"]})
    for f in tests.get("security", {}).get("findings", []):
        out.append({"severity": "blocking" if f["severity"] == "high" else "major", "rule": f["rule"],
                    "title": f"Security/scope: {f['rule']}", "detail": f["detail"]})
    return [_norm(f, "deterministic-tests") for f in out]


def aggregate(reviews: list[dict], tests: dict, confirm_probe: Callable[[str], tuple[bool, str]]) -> dict:
    """Merge reviewer + test findings into actionable, evidence-backed feedback items.

    Reviewer findings that carry an executable probe are re-executed against the current code:
    a probe that *passes* means the claimed defect is not reproducible, so the finding is marked
    unconfirmed and downgraded from blocking to major (this guards against hallucinated findings).
    """
    findings = test_findings(tests)
    for r in reviews:
        if r.get("verdict") == "error":
            continue
        findings += [_norm(f, r["reviewer"]) for f in r.get("findings", [])]

    for f in findings:
        if f["probe"] and f["reviewer"] != "deterministic-tests":
            still_fails = not confirm_probe(f["probe"])[0]
            f["confirmed"] = still_fails
            if not still_fails:
                f["detail"] += " [unconfirmed: probe passes on current code]"
                if f["severity"] == "blocking":
                    f["severity"] = "major"

    groups: dict[tuple, list[dict]] = {}
    for f in findings:
        loc = f["function"] or (f"line {f['line']}" if f["line"] else "general")
        groups.setdefault((f["file"] or "", loc), []).append(f)

    items = []
    for (file, loc), fs in groups.items():
        fs.sort(key=lambda f: SEVERITY_RANK[f["severity"]])
        sources = sorted({f["reviewer"] for f in fs})
        items.append({
            "severity": fs[0]["severity"], "title": fs[0]["title"],
            "location": {"file": file or None, "where": loc,
                         "lines": sorted({f["line"] for f in fs if f["line"]})},
            "sources": sources, "corroborated": len(sources) > 1,
            "rules": sorted({f["rule"] for f in fs}),
            "probes": sorted({f["probe"] for f in fs if f["probe"] and f["severity"] == "blocking"
                              and f["confirmed"] is not False}),
            "suggestions": [f["suggestion"] for f in fs if f["suggestion"]],
            "findings": fs,
        })
    items.sort(key=lambda i: (SEVERITY_RANK[i["severity"]], i["location"]["file"] or "", i["location"]["where"]))
    for k, item in enumerate(items, 1):
        item["id"] = f"F{k}"

    blocking = [i for i in items if i["severity"] == "blocking"]
    signature = None
    if blocking:
        keys = sorted([i["location"]["file"] or "", i["location"]["where"],
                       sorted(f["rule"] for f in i["findings"] if f["severity"] == "blocking")] for i in blocking)
        signature = hashlib.sha1(json.dumps(keys).encode()).hexdigest()[:12]
    restart = any(set(i["rules"]) & RESTART_RULES for i in blocking)
    lines: dict[str, list[int]] = {}
    functions: set[str] = set()
    for i in items:
        if i["location"]["file"]:
            lines.setdefault(i["location"]["file"], []).extend(i["location"]["lines"])
        for f in i["findings"]:
            if f["function"]:
                functions.add(f["function"])
    return {"items": items, "blocking_count": len(blocking), "signature": signature,
            "strategy": "restart_from_checkpoint" if restart else "build_on_previous",
            "probes": sorted({p for i in blocking for p in i["probes"]}),
            "focus": {"lines": {k: sorted(set(v)) for k, v in lines.items()}, "functions": sorted(functions)}}


def render_feedback(n: int, agg: dict, decision: str, reason: str) -> str:
    out = [f"Iteration {n} — decision: {decision.upper()}", reason, ""]
    for i in agg["items"]:
        loc = i["location"]
        where = f"{loc['file']}::{loc['where']}" if loc["file"] else loc["where"]
        out.append(f"[{i['id']}] [{i['severity'].upper()}] {where} — {i['title']}")
        out.append(f"     sources: {', '.join(i['sources'])}" + ("  (corroborated)" if i["corroborated"] else ""))
        for f in i["findings"]:
            conf = {True: " (confirmed by re-execution)", False: " (unconfirmed)", None: ""}[f["confirmed"]]
            out.append(f"     - {f['reviewer']}: [{f['severity']}] {f['title']}{conf}")
            if f["detail"]:
                out.append(f"       {f['detail'][:300]}")
        for s in i["suggestions"]:
            out.append(f"     suggestion: {s}")
        for p in i["probes"]:
            out.append("     required probe:\n" + "\n".join("       " + ln for ln in p.strip().splitlines()))
    if not agg["items"]:
        out.append("No findings.")
    out.append("")
    out.append(f"Revision strategy for next attempt: {agg['strategy']}")
    return "\n".join(out)


# ------------------------------------------------------------------ progress / stall detection
def assess_progress(previous: list[dict], current: dict, budget: dict, usage: dict,
                    loop_started_at: float | None, now: float | None = None) -> tuple[str, str]:
    """Decide the outcome of an iteration.

    previous: closed earlier iterations, each {n, fingerprint, failure_signature, blocking_count}.
    current:  {n, fingerprint, failure_signature, blocking_count}.
    Returns (decision, reason) with decision in pass | revise | stalled | exhausted.
    """
    now = now or time.time()
    n, blocking = current["n"], current["blocking_count"]
    if blocking == 0:
        return "pass", f"Iteration {n}: no blocking findings and all deterministic checks are green."
    same = [p for p in previous if p.get("fingerprint") and p["fingerprint"] == current.get("fingerprint")]
    if same:
        return "stalled", (f"Repeated solution: iteration {n} produced the same patch as iteration "
                           f"{same[-1]['n']} (fingerprint {current['fingerprint'][:10]}).")
    if previous and previous[-1].get("failure_signature") == current.get("failure_signature"):
        return "stalled", (f"Identical failure pattern: iteration {n} has the same blocking findings as "
                           f"iteration {previous[-1]['n']} (signature {current['failure_signature']}).")
    window = int(budget.get("stall_window", 2))
    seq = [p["blocking_count"] for p in previous if p.get("blocking_count") is not None] + [blocking]
    if len(seq) > window and all(seq[i] >= seq[i - 1] for i in range(len(seq) - window, len(seq))):
        return "stalled", (f"No progress: blocking findings did not decrease over the last {window} "
                           f"iterations (counts {seq}).")
    max_it = int(budget.get("max_iterations", 4))
    if n >= max_it:
        return "exhausted", f"Iteration limit reached ({n}/{max_it}) with {blocking} blocking finding(s) open."
    max_loop = budget.get("max_loop_seconds")
    if max_loop and loop_started_at is not None and now - loop_started_at > max_loop:
        return "exhausted", f"Loop time budget exhausted ({now - loop_started_at:.0f}s > {max_loop}s)."
    if usage.get("cost_usd", 0) >= budget.get("max_cost_usd", float("inf")):
        return "exhausted", f"Cost budget exhausted (${usage['cost_usd']:.4f} >= ${budget['max_cost_usd']:.2f})."
    if usage.get("llm_tokens", 0) >= budget.get("max_llm_tokens", float("inf")):
        return "exhausted", f"Token budget exhausted ({usage['llm_tokens']} >= {budget['max_llm_tokens']})."
    return "revise", f"Iteration {n}: {blocking} blocking finding(s); sending feedback to the coding agent."
