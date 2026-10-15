"""Sanitized, shareable evidence packages for a workflow.

    summary.md          human-readable account of what happened, with reproducible commands
    run-manifest.json   environment, configuration, exact models + digests, revisions, termination reason
    iterations.json     every coding/review iteration: patch, fingerprint, tests, feedback, decision
    review-findings.json every reviewer call: provider, model, real/simulated, findings, executed checks
    test-results.json   every test command that actually ran: commit, exit code, pass/fail counts
    quality-gate.json   gate checks, acceptance-criteria mapping, approvals, final state
    patch.diff          final diff of the workflow branch against its base commit
    llm-calls.json      every model call (status, timings, tokens reported by the runtime, prompt/response)
    tool-calls.json     every coding-agent tool action and its result
    events.json         the audit log

Everything is passed through `sanitize()`: secrets are redacted and absolute local paths are rewritten
to <PROJECT>, <DATA_DIR> or <HOME>, so packages can be shared without leaking the machine layout.
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import PROJECT_ROOT
from .llm import OllamaClient, redact

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
ALLOWED_EMAILS = {"agent@systemfactory.local"}


def _path_variants(p: Path) -> list[str]:
    s = str(p.resolve())
    return sorted({s, s.replace("\\", "/"), s.replace("\\", "\\\\")}, key=len, reverse=True)


class Sanitizer:
    def __init__(self, data_dir: Path):
        self.repl = []
        for p, tag in ((data_dir, "<DATA_DIR>"), (PROJECT_ROOT, "<PROJECT>"), (Path.home(), "<HOME>")):
            for v in _path_variants(p):
                self.repl.append((re.compile(re.escape(v), re.I), tag))
        user = os.environ.get("USERNAME") or os.environ.get("USER")
        self.user = re.compile(rf"(?i)([\\/]Users[\\/]){re.escape(user)}\b") if user else None

    def text(self, s: str) -> str:
        s = redact(s)
        for rx, tag in self.repl:
            s = rx.sub(tag, s)
        if self.user:
            s = self.user.sub(r"\1<USER>", s)
        return EMAIL.sub(lambda m: m.group(0) if m.group(0) in ALLOWED_EMAILS else "<email>", s)

    def __call__(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self(v) for v in obj]
        return obj


def _git(cwd, *args) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=30)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _gpu() -> str | None:
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def termination(wf: dict, iterations: list[dict]) -> str:
    if wf["state"] == "approved":
        return "approved by a human after quality gate SUCCESS"
    if wf["state"] == "awaiting_approval":
        return "quality gate SUCCESS; waiting for human approval"
    if wf.get("error"):
        return wf["error"]
    if iterations and iterations[-1].get("decision_reason"):
        return iterations[-1]["decision_reason"]
    return wf["state"]


def collect(orch, wf_id: int) -> dict[str, object]:
    """Build every evidence document for a workflow (unsanitized)."""
    st = orch.store
    wf = st.get_workflow(wf_id)
    if wf is None:
        raise KeyError(f"workflow {wf_id} not found")
    its, reviews = st.get_iterations(wf_id), st.get_reviews(wf_id)
    calls, tools, tests = st.get_llm_calls(wf_id, with_text=True), st.get_tool_calls(wf_id), st.get_test_runs(wf_id)
    events, evidence, approvals = st.get_events(wf_id), st.get_evidence(wf_id), st.get_approvals(wf_id)
    data = wf["data"]
    head = _git(wf["worktree"], "rev-parse", "HEAD") if wf.get("worktree") and Path(wf["worktree"]).exists() else None
    diff = (_git(wf["worktree"], "diff", f"{wf['base_commit']}..HEAD") if head and wf.get("base_commit") else None) or \
        next((e["content"] for e in reversed(evidence) if e["kind"] in ("diff", "patch")), "")
    models = {}
    for c in calls:
        m = models.setdefault(c["model"], {"provider": c["provider"], "execution": c["execution"],
                                           "digest": c["model_digest"], "details": c["model_details"],
                                           "roles": set(), "calls": 0, "ok": 0, "input_tokens": 0, "output_tokens": 0,
                                           "runtime_seconds": 0.0})
        m["roles"].add(c["role"]); m["calls"] += 1; m["ok"] += c["status"] == "ok"
        m["input_tokens"] += c["input_tokens"] or 0; m["output_tokens"] += c["output_tokens"] or 0
        m["runtime_seconds"] += c["runtime_seconds"] or 0.0
    for m in models.values():
        m["roles"] = sorted(m["roles"])
    executions = sorted({c["execution"] for c in calls} | {"simulated" if r["simulated"] else r.get("execution") or "real"
                                                            for r in reviews})
    if not calls and wf["options"].get("runtime", "simulated") == "simulated":
        executions = sorted(set(executions) | {"simulated"})
    ollama = OllamaClient(base_url=wf["options"].get("ollama_url")).status(sorted(models)) if models else None
    sample = (wf.get("issue_source") or "").removeprefix("sample:").split("+")[0]
    runtime = wf["options"].get("runtime", "simulated")
    repro = [f"python -m systemfactory init-demo",
             f"python -m systemfactory submit --sample {sample} --runtime {runtime}" if sample else
             "python -m systemfactory submit --title ... --body ...",
             "python -m systemfactory run", f"python -m systemfactory show {wf_id}",
             f"python -m systemfactory export {wf_id}"]
    if wf.get("branch"):
        repro.append(f"git -C <DATA_DIR>/repos/shopcart log --oneline {wf['base_commit'][:10]}..{wf['branch']}")
    totals = {"llm_calls": len(calls), "llm_calls_ok": sum(c["status"] == "ok" for c in calls),
              "llm_calls_failed": sum(c["status"] != "ok" for c in calls),
              "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
              "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
              "inference_seconds": round(sum((c["finished_at"] or c["started_at"]) - c["started_at"] for c in calls), 2),
              "tool_calls": len(tools), "test_runs": len(tests),
              "automation_seconds": round((wf["finished_at"] or time.time()) - wf["started_at"], 2) if wf["started_at"] else None}
    manifest = {
        "system_factory": {"version": __version__, "git_revision": _git(PROJECT_ROOT, "rev-parse", "HEAD"),
                           "git_dirty": bool(_git(PROJECT_ROOT, "status", "--porcelain"))},
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "gpu": _gpu(),
                        "ollama": {k: ollama.get(k) for k in ("url", "reachable", "version")} if ollama else None},
        "workflow": {"id": wf["id"], "issue_id": wf["issue_id"], "title": wf["issue_title"], "source": wf["issue_source"],
                     "kind": wf["kind"], "state": wf["state"], "runtime": runtime, "options": wf["options"],
                     "budget": wf["budget"], "usage": wf["usage"], "branch": wf.get("branch"),
                     "base_commit": wf.get("base_commit"), "final_commit": head,
                     "created_at": wf["created_at"], "started_at": wf["started_at"], "finished_at": wf["finished_at"]},
        "execution_modes": executions,
        "models": models,
        "reviewer_diversity": (data.get("review") or {}).get("diversity"),
        "iterations": len(its), "termination_reason": termination(wf, its),
        "quality_gate": (data.get("quality_gate") or {}).get("status"),
        "totals": totals, "reproduce": repro,
        "honesty": "Token counts are copied from the runtime's responses; nothing is estimated. Simulated "
                   "components are labelled 'simulated'. Different models on one runtime are one provider.",
    }
    agg_checks = {it["n"]: (it.get("feedback") or {}).get("checks", []) for it in its}
    review_docs = [{"iteration": r["iteration"], "reviewer": r["reviewer"], "provider": r["provider"],
                    "model": r["model"], "model_digest": r.get("model_digest"),
                    "execution": r.get("execution") or ("simulated" if r["simulated"] else "real"),
                    "simulated": r["simulated"], "verdict": r["verdict"], "summary": r["summary"],
                    "analysis": r.get("analysis"), "findings": r["findings"],
                    "checks_executed": [c for c in agg_checks.get(r["iteration"], []) if c["reviewer"] == r["reviewer"]],
                    "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"], "seconds": r["seconds"],
                    "error": r["error"], "llm_call_id": r.get("llm_call_id")} for r in reviews]
    it_docs = [{**{k: it[k] for k in ("n", "status", "base", "commit", "fingerprint", "patch", "decision",
                                     "decision_reason", "failure_signature", "blocking_count", "started_at", "finished_at")},
                "tests": {k: v for k, v in (it.get("tests") or {}).items() if k not in ("repro_output", "suite_output")},
                "feedback": it.get("feedback"),
                "llm_call_ids": [c["id"] for c in calls if c["iteration"] == it["n"]],
                "tool_calls": [t for t in tools if t["iteration"] == it["n"]]} for it in its]
    gate = {"status": (data.get("quality_gate") or {}).get("status") or ("not reached" if wf["state"] != "failed" else "FAILED"),
            "gate": data.get("quality_gate"),
            "gate_report": next((e["content"] for e in reversed(evidence) if e["kind"] == "quality_gate"), None),
            "escalation": data.get("escalation"), "approvals": approvals, "final_state": wf["state"],
            "termination_reason": termination(wf, its)}
    summary = _summary(wf, manifest, it_docs, review_docs, tests, gate, totals)
    return {"summary.md": summary, "run-manifest.json": manifest, "iterations.json": it_docs,
            "review-findings.json": review_docs, "test-results.json": tests, "quality-gate.json": gate,
            "patch.diff": diff or "", "llm-calls.json": calls, "tool-calls.json": tools, "events.json": events}


def _summary(wf, manifest, its, reviews, tests, gate, totals) -> str:
    L = [f"# Evidence: workflow {wf['id']} — {wf['issue_title']}", "",
         f"- **Final state:** `{wf['state']}` — {gate['termination_reason']}",
         f"- **Runtime:** `{manifest['workflow']['runtime']}`; execution modes recorded: "
         f"{', '.join(manifest['execution_modes']) or 'none'}",
         f"- **Quality gate:** {gate['status']}",
         f"- **Iterations:** {len(its)}; LLM calls {totals['llm_calls']} ({totals['llm_calls_failed']} failed); "
         f"tokens in/out {totals['input_tokens']}/{totals['output_tokens']}; inference {totals['inference_seconds']}s; "
         f"test runs {totals['test_runs']}; automation time {totals['automation_seconds']}s",
         f"- **Reviewer diversity:** {(manifest['reviewer_diversity'] or {}).get('label', 'n/a')}", ""]
    if manifest["models"]:
        L += ["## Models", "", "| model | provider | execution | digest | roles | calls ok/total | tokens in/out |",
              "|---|---|---|---|---|---|---|"]
        for name, m in manifest["models"].items():
            L.append(f"| `{name}` | {m['provider']} | {m['execution']} | `{(m['digest'] or '')[:12]}` | "
                     f"{', '.join(m['roles'])} | {m['ok']}/{m['calls']} | {m['input_tokens']}/{m['output_tokens']} |")
        L.append("")
    for it in its:
        p = it["patch"] or {}
        L += [f"## Iteration {it['n']} — {it['status']}", "",
              f"- Patch: {p.get('file', '-')} — {p.get('mutation', '-')} (coder `{p.get('coder')}`"
              + (f", model `{p.get('model')}`, {p.get('turns')} turns" if p.get("model") else "") + ")",
              f"- Fingerprint `{(it['fingerprint'] or '-')[:16]}`, strategy `{p.get('strategy')}`, commit `{(it['commit'] or '-')[:10]}`",
              f"- Tests: repro {'pass' if it['tests'].get('repro_passed') else 'FAIL'}, suite "
              f"{'pass' if it['tests'].get('suite_passed') else 'FAIL'} ({it['tests'].get('suite_summary', '')})"]
        for r in [r for r in reviews if r["iteration"] == it["n"]]:
            ex = r["checks_executed"]
            L.append(f"- Reviewer **{r['reviewer']}** `{r['provider']}/{r['model']}` ({r['execution']}): **{r['verdict']}**, "
                     f"{len(r['findings'])} finding(s), {sum(1 for c in ex if c['outcome'] in ('mismatch', 'exception'))}"
                     f"/{len(ex)} executed checks failed")
        for i in (it["feedback"] or {}).get("items", []):
            L.append(f"  - `{i['id']}` [{i['severity']}] {i['location'].get('file') or ''} {i['location']['where']}: "
                     f"{i['title']} (sources: {', '.join(i['sources'])})")
        L += [f"- Decision: **{it['decision']}** — {it['decision_reason']}", ""]
    L += ["## Test commands that ran", "", "| step | iteration | purpose | exit | passed | failed | commit |",
          "|---|---|---|---|---|---|---|"]
    for t in tests:
        L.append(f"| {t['step']} | {t['iteration'] or '-'} | {t['purpose']} | {t['exit_code']} | {t['passed']} | "
                 f"{t['failed']} | `{(t['commit_sha'] or '')[:10]}` |")
    L += ["", "## Reproduce", "", "```bash", *manifest["reproduce"], "```", "",
          "_Generated by System Factory. Paths are sanitized; token counts come from the runtime._"]
    return "\n".join(L) + "\n"


def export(orch, wf_id: int, out_dir: Path | str | None = None) -> Path:
    docs = collect(orch, wf_id)
    clean = Sanitizer(orch.settings.data_dir)
    out = Path(out_dir) if out_dir else orch.settings.data_dir / "evidence" / f"wf-{wf_id}"
    out.mkdir(parents=True, exist_ok=True)
    for name, content in docs.items():
        content = clean(content)
        text = content if isinstance(content, str) else json.dumps(content, indent=2, ensure_ascii=False, default=str)
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    return out


def export_zip(orch, wf_id: int) -> bytes:
    docs = collect(orch, wf_id)
    clean = Sanitizer(orch.settings.data_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in docs.items():
            content = clean(content)
            z.writestr(f"wf-{wf_id}/{name}", content if isinstance(content, str)
                       else json.dumps(content, indent=2, ensure_ascii=False, default=str))
    return buf.getvalue()
