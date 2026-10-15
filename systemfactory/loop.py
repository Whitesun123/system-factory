"""Implementation-loop logic: aggregation of review findings and progress / stall detection.

Everything here is pure (no I/O except through callables passed in), so the rules that decide
"revise", "pass", "stalled" or "exhausted" are unit-tested directly.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import time
from typing import Callable

SEVERITY_RANK = {"blocking": 0, "unresolved": 1, "major": 2, "minor": 3}

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


PROBE_SETUP_ERRORS = ("NameError", "ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError",
                      "AttributeError", "TypeError")


def classify_probe(ok: bool, output: str) -> str:
    """pass | mismatch (wrong result: the defect is real) | exception (code raised at runtime) |
    probe-error (the probe itself is broken: bad import/name/signature) | invalid (no runnable example)."""
    if ok:
        return "pass"  # includes a probe with no runnable examples: it proves nothing either way
    if "Expected:" in output and "Got:" in output and "Exception raised" not in output:
        return "mismatch"
    m = re.search(r"Exception raised:[\s\S]*?\n\s*(\w+(?:\.\w+)*):", output)
    if m:
        return "probe-error" if m.group(1).split(".")[-1] in PROBE_SETUP_ERRORS else "exception"
    if "Traceback" in output or "Error" in output:
        return "probe-error"
    return "invalid"


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
    sev = sev if sev in ("blocking", "major", "minor") else "major"  # "unresolved" is assigned only internally
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


TRUSTED_REVIEWERS = {"simulated-spec", "simulated-diff"}  # deterministic reviewers written by System Factory

_STOP = set("the a an and or to of in on for is are be by with as at it this that from must should will "
            "returns return result results value values function when if than then".split())


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", (text or "").lower()) if len(t) > 1 and t not in _STOP}


def requirement_sentences(texts: list[str]) -> list[str]:
    out = []
    for t in texts:
        for part in re.split(r"(?<=[.!?])\s+|\n+", t or ""):
            part = part.strip(" -*#>`")
            if len(part) > 3:
                out.append(part)
    return out


def ground(claim: str, expected: str | None, sentences: list[str] | None) -> str | None:
    """Return the authoritative sentence that justifies a reviewer's expectation, or None.

    Grounded when (a) the expected value itself appears in an authoritative sentence (e.g. a documented example or
    message), or (b) at least half (and at least 3) of the claim's content words appear in one sentence.
    """
    if sentences is None:
        return "grounding not applied"
    raw = (expected or "").strip()
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        value = raw
    # Value grounding only for text results, and only as an exact quoted string or a whole sentence; numbers and
    # substrings appear everywhere by coincidence and justify nothing.
    if isinstance(value, str) and len(value) >= 4:
        quoted = (f"'{value}'", f'"{value}"')
        for s in sentences:
            if any(q in s for q in quoted) or s.strip().rstrip(".!") == value.strip().rstrip(".!"):
                return s
    ct = _tokens(claim)
    if not ct:
        return None
    best, score = None, 0.0
    for s in sentences:
        common = ct & _tokens(s)
        r = len(common) / len(ct)
        if len(common) >= 3 and r > score:
            best, score = s, r
    return best if score >= 0.5 else None


def _probe_key(probe: str) -> tuple[str, str] | None:
    """(normalised call, normalised expected) from a single-call doctest probe."""
    lines = [ln for ln in probe.strip().splitlines() if ln.strip()]
    calls = [ln[4:].strip() for ln in lines if ln.startswith(">>> ") and not ln[4:].lstrip().startswith(("from ", "import "))]
    wants = [ln.strip() for ln in lines if not ln.startswith((">>>", "..."))]
    if len(calls) != 1 or len(wants) != 1:
        return None
    return calls[0].replace(" ", ""), wants[0]


def aggregate(reviews: list[dict], tests: dict, confirm_probe: Callable[[str], tuple[bool, str]],
              requirements: list[str] | None = None, changed_functions: set[str] | None = None) -> dict:
    """Merge reviewer + test findings into evidence-classified, actionable feedback items.

    Evidence states for reviewer claims:
      confirmed    executed failure whose expectation is grounded in a requirement or corroborated by another
                   reviewer -> blocking
      unsupported  executed failure, expectation not traceable to any requirement     -> unresolved
      conflicting  reviewers expect different results for the same call               -> unresolved
      unverified   claim without an executable probe, or probe passes / is broken     -> downgraded
      invalid      check rejected by validation                                       -> recorded only
      passed       executed and passing                                               -> recorded only
    `requirements` are the authoritative texts (issue, acceptance criteria, docstrings, README); None disables
    grounding (unit tests of other rules only).
    """
    sentences = requirement_sentences(requirements) if requirements is not None else None
    findings = test_findings(tests)
    for f in findings:
        f["evidence_state"] = "deterministic"
    checks_run, inspected = [], {}
    for r in reviews:
        if r.get("verdict") == "error":
            continue
        trusted = r["reviewer"] in TRUSTED_REVIEWERS
        fs = [_norm(f, r["reviewer"]) for f in r.get("findings", [])]
        findings += fs
        valid_checks = 0
        for c in r.get("checks", []) or []:
            row = {"reviewer": r["reviewer"], "function": c.get("function"), "call": c.get("call"),
                   "expected": c.get("expected"), "rule": c.get("rule"), "valid": bool(c.get("probe")),
                   "probe": c.get("probe"), "file": c.get("file"), "invalid_reason": c.get("invalid_reason")}
            if row["valid"]:
                valid_checks += 1
                ok, output = confirm_probe(c["probe"])
                row["outcome"] = classify_probe(ok, output)
                got = re.search(r"Got:\s*\n\s*(.+)", output)
                row["got"] = got.group(1).strip() if got else None
                row["output"] = output[-1200:]
                row["key"] = _probe_key(c["probe"])
            else:
                row["outcome"] = "invalid"
            checks_run.append(row)
        # A review counts as an inspection only if it did observable work on the changed code.
        if trusted:
            inspected[r["reviewer"]] = "deterministic technique"
        elif valid_checks and (changed_functions is None or any(
                row["function"] in changed_functions for row in checks_run if row["reviewer"] == r["reviewer"] and row["valid"])):
            inspected[r["reviewer"]] = f"{valid_checks} valid check(s) executed"
        elif any(f["file"] or f["function"] for f in fs):
            inspected[r["reviewer"]] = "located findings"

    # Cross-reviewer agreement and conflicts on identical calls.
    by_key: dict[tuple, list[dict]] = {}
    for row in checks_run:
        if row.get("key"):
            by_key.setdefault((row["function"], row["key"][0]), []).append(row)
    for rows in by_key.values():
        expectations = {row["key"][1] for row in rows}
        for row in rows:
            row["conflicting"] = len(expectations) > 1
            row["corroborated_by"] = sorted({o["reviewer"] for o in rows if o is not row
                                             and o["key"][1] == row["key"][1] and o["reviewer"] != row["reviewer"]})

    for row in checks_run:
        if row["outcome"] not in ("mismatch", "exception"):
            row["evidence_state"] = {"pass": "passed", "invalid": "invalid"}.get(row["outcome"], "invalid")
            continue
        justification = ground(row.get("rule") or "", row.get("expected"), sentences)
        row["grounding"] = justification
        if row.get("conflicting"):
            state, sev = "conflicting", "unresolved"
        elif justification or row.get("corroborated_by"):
            state, sev = "confirmed", "blocking"
        else:
            state, sev = "unsupported", "unresolved"
        row["evidence_state"] = state
        why = {"confirmed": f"Justified by: \"{(justification or '')[:160]}\"" if justification and justification != "grounding not applied"
               else (f"Corroborated by {', '.join(row['corroborated_by'])}" if row.get("corroborated_by") else ""),
               "conflicting": "Reviewers expect different results for this call; a human must decide which is right.",
               "unsupported": "The expected value is not traceable to the issue, acceptance criteria or documentation."}[state]
        f = _norm({"severity": "major", "rule": "reviewer-check", "file": row.get("file"), "function": row["function"],
                   "probe": row["probe"], "title": f"Specification check failed: {row['call']} should be {row['expected']}",
                   "detail": f"Executed against the patched code: returned {row['got'] or row['outcome']}. "
                             f"Rule: {row.get('rule') or ''}. {why}",
                   "evidence": row["output"], "suggestion": f"Make {row['call']} return {row['expected']}."
                   if state == "confirmed" else "Confirm the intended behaviour against the requirements."},
                  row["reviewer"])
        f["severity"] = sev
        f["confirmed"], f["probe_outcome"], f["prechecked"], f["evidence_state"] = state == "confirmed", row["outcome"], True, state
        findings.append(f)

    for f in findings:
        if f.get("prechecked") or f["reviewer"] == "deterministic-tests":
            continue
        trusted = f["reviewer"] in TRUSTED_REVIEWERS
        if f["probe"]:
            ok, output = confirm_probe(f["probe"])
            outcome = classify_probe(ok, output)
            f["probe_outcome"] = outcome
            if outcome in ("mismatch", "exception"):
                key = _probe_key(f["probe"])
                justification = "deterministic reviewer" if trusted else ground(
                    f"{f['title']} {f['detail']}", key[1] if key else None, sentences)
                if justification:
                    f["confirmed"], f["evidence_state"] = True, "confirmed"
                    if f["severity"] != "blocking":
                        f["detail"] += f" [upgraded to blocking: probe confirmed by execution; reviewer said {f['severity']}]"
                        f["reviewer_severity"], f["severity"] = f["severity"], "blocking"
                else:
                    f["confirmed"], f["evidence_state"] = False, "unsupported"
                    f["detail"] += " [unresolved: probe fails, but its expectation is not traceable to any requirement]"
                    f["severity"] = "unresolved"
            else:
                f["confirmed"], f["evidence_state"] = False, "unverified"
                why = {"pass": "probe passes on current code", "invalid": "probe is not a valid check",
                       "probe-error": "probe itself errors (" + output.strip().splitlines()[-1][:120] + ")"
                       if output.strip() else "probe errored"}.get(outcome, outcome)
                f["detail"] += f" [unconfirmed: {why}]"
                if f["severity"] == "blocking":
                    f["severity"] = "major"
        elif f["severity"] == "blocking" and not trusted:
            # a model's claim is not proof: a blocking finding needs an executable probe that fails
            f["confirmed"], f["evidence_state"] = False, "unverified"
            f["detail"] += " [unverified: blocking finding without an executable probe]"
            f["severity"] = "major"
        else:
            f.setdefault("evidence_state", "deterministic" if trusted else "unverified")

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
            "evidence_states": sorted({f.get("evidence_state", "unverified") for f in fs}),
            "probes": sorted({f["probe"] for f in fs if f["probe"] and f["severity"] == "blocking"
                              and f["confirmed"] is not False}),
            "suggestions": [f["suggestion"] for f in fs if f["suggestion"]],
            "findings": fs,
        })
    items.sort(key=lambda i: (SEVERITY_RANK[i["severity"]], i["location"]["file"] or "", i["location"]["where"]))
    for k, item in enumerate(items, 1):
        item["id"] = f"F{k}"

    blocking = [i for i in items if i["severity"] == "blocking"]
    unresolved = [i for i in items if i["severity"] == "unresolved"]
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
    for row in checks_run:
        row.pop("output", None)
    return {"items": items, "blocking_count": len(blocking), "unresolved_count": len(unresolved),
            "signature": signature, "checks": checks_run, "inspected_reviews": inspected,
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
        unresolved = current.get("unresolved_count", 0)
        if unresolved:
            return "unresolved", (f"Iteration {n}: no confirmed defect, but {unresolved} finding(s) have unsupported or "
                                  "conflicting evidence; a human must decide instead of approving or blocking silently.")
        inspected, needed = current.get("inspected"), current.get("min_reviewers")
        if inspected is not None and needed and inspected < needed:
            return "unresolved", (f"Iteration {n}: only {inspected} of the required {needed} reviews inspected the "
                                  "change (no executed checks or located findings); an approval without inspection "
                                  "does not count.")
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
