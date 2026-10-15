"""Reviewer evaluation harness. Uses the pipeline's own functions (aggregate, scan_diff_text, requirement_texts,
reviewer adapters with probe vetting, execution backends), so it measures the real decision path.

Decisions compared per case:
    raw       what the model said: "block" if verdict == request_changes, else "approve"
    pipeline  what System Factory would do after evidence verification: "block" (confirmed blocking finding),
              "escalate" (unresolved evidence or too few inspected reviews), or "approve"
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.dataset import build_case  # noqa: E402
from systemfactory import loop, reviewers  # noqa: E402
from systemfactory.execution import LocalExecutionBackend, make_backend  # noqa: E402
from systemfactory.roles import BUG_ACCEPTANCE_CRITERIA, REPRO_TEMPLATE, extract_example, prose, requirement_texts, \
    scan_diff_text  # noqa: E402
from systemfactory.sandbox import Git, scrubbed_env  # noqa: E402

LABELS = json.loads((ROOT / "evaluation" / "labels.json").read_text(encoding="utf-8"))
PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["."]\n'


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def prompt_hash(role: str) -> str:
    focus = reviewers.REVIEWER_FOCUS.get(role, ("", "", ""))
    return sha(reviewers.OLLAMA_REVIEW_SYSTEM + "|".join(focus) + reviewers.CHECKS_INSTRUCTIONS +
               json.dumps(reviewers.OLLAMA_REVIEW_SCHEMA, sort_keys=True))


def case_hash(case: dict) -> str:
    return sha(json.dumps({k: case[k] for k in ("issue", "path", "before", "after", "tests")}, sort_keys=True))


class Materialised:
    """A case as a real git repository: base commit, reproduction-test commit (checkpoint), patch commit."""

    def __init__(self, case: dict, workdir: Path, gitconfig: Path, backend_name: str = "local"):
        self.case, self.dir = case, workdir
        self._probe_cache: dict[str, tuple[bool, str]] = {}
        self.repo, self.probes = workdir / "repo", workdir / "probes"
        self.repo.mkdir(parents=True)
        self.probes.mkdir()
        self.git = Git(gitconfig)
        self.backend = LocalExecutionBackend() if backend_name == "local" else make_backend(backend_name)
        r = self.repo
        (r / "shopkit").mkdir()
        (r / "tests").mkdir()
        (r / "shopkit" / "__init__.py").write_text("", encoding="utf-8")
        (r / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
        self._write(case["path"], case["before"])
        self._write(case["tests_path"], case["tests"])
        self.git.run(r, "init", "-q", "-b", "main")
        self.git.run(r, "add", "-A")
        self.git.run(r, "commit", "-q", "-m", "base")
        example = extract_example(case["issue"]["body"])
        self._write("tests/test_issue_1.py", REPRO_TEMPLATE.format(issue_id=1, title=case["issue"]["title"],
                                                                   example=example))
        self.git.run(r, "add", "-A")
        self.git.run(r, "commit", "-q", "-m", "repro")
        self.checkpoint = self.git.out(r, "rev-parse", "HEAD")

    def _write(self, rel: str, text: str) -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def pytest(self, *paths: str):
        return self.backend.run_python(["-m", "pytest", "-q", "-p", "no:cacheprovider", *paths], workdir=self.repo,
                                       ro_dirs={self.probes: "/sf/probes"}, timeout=60, should_stop=None)

    def apply_patch(self) -> None:
        self._write(self.case["path"], self.case["after"])
        self.git.run(self.repo, "commit", "-q", "-am", "patch")
        self.head = self.git.out(self.repo, "rev-parse", "HEAD")

    def run_probe(self, source: str) -> tuple[bool, str]:
        # deterministic for a fixed patch: memoise so panels do not re-run identical executions
        if source in self._probe_cache:
            return self._probe_cache[source]
        self._probe_cache[source] = result = self._execute_probe(source)
        return result

    def _execute_probe(self, source: str) -> tuple[bool, str]:
        path = self.probes / f"{loop.probe_id(source)}.txt"
        path.write_text(source, encoding="utf-8")
        res = self.backend.run_python(["-c", loop.PROBE_RUNNER, str(path)], workdir=self.repo,
                                      ro_dirs={self.probes: "/sf/probes"}, timeout=20, should_stop=None)
        return res.ok, res.output

    def oracle_passes(self) -> bool:
        code = LABELS["oracles"][self.case["base"]]
        r = subprocess.run([sys.executable, "-c", code], cwd=self.repo, capture_output=True, text=True,
                           env=scrubbed_env(extra={"PYTHONPATH": str(self.repo)}), timeout=30)
        return r.returncode == 0

    def bundle(self) -> dict:
        path = self.case["path"]
        repro, suite = self.pytest("tests/test_issue_1.py"), self.pytest()

        def tail(output: str) -> str:
            return (output.strip().splitlines() or [""])[-1]
        diff = self.git.out(self.repo, "diff", f"{self.checkpoint}..HEAD", "--", path)
        tests = {"repro_passed": repro.ok, "suite_passed": suite.ok, "repro_summary": tail(repro.output),
                 "suite_summary": tail(suite.output), "repro_output": repro.output[-1500:],
                 "suite_output": suite.output[-1500:], "probes": [], "probes_passed": True,
                 "security": scan_diff_text([path], diff, [path])}
        tests["all_passed"] = repro.ok and suite.ok and tests["security"]["passed"]
        issue = self.case["issue"]
        return {"iteration": 1, "issue": dict(issue),
                "prd": {"title": issue["title"], "type": "bug", "problem": prose(issue["body"]),
                        "acceptance_criteria": list(BUG_ACCEPTANCE_CRITERIA)},
                "plan": {"target_files": [path], "steps": []}, "diff": diff,
                "diff_u0": self.git.out(self.repo, "diff", "-U0", f"{self.checkpoint}..HEAD", "--", path),
                "changed_files": [path], "files_after": {path: self.case["after"]}, "tests": tests,
                "fingerprint": loop.fingerprint({path: self.case["after"]}), "patch": {}, "previous_iterations": []}


class _CallLog:
    """Recorder target: keeps model-call records in memory (digest, tokens, timings) instead of a workflow DB."""

    def __init__(self):
        self.calls = []

    def add_llm_call(self, rec):
        self.calls.append(rec)
        return len(self.calls)


def run_reviewer(name: str, model: str | None, bundle: dict, tools) -> dict:
    """One review, returning a normalised record (vetted the same way as the review panel)."""
    from systemfactory.llm import Recorder
    log = _CallLog()
    adapter: reviewers.Reviewer
    if name in reviewers.REVIEWER_FOCUS:
        if model is None:
            raise ValueError(f"reviewer {name} needs a model")
        ollama = reviewers.OllamaReviewer(name, model)
        ollama.recorder = Recorder(log, 0, 1, "review", name)
        adapter = ollama
    else:
        adapter = reviewers.make(name)
    t0 = time.time()
    rec = {"reviewer": name, "model": model or adapter.model, "provider": adapter.provider,
           "simulated": adapter.simulated}
    try:
        res = adapter.review(copy.deepcopy(bundle), tools)
        findings = res.get("findings", [])
        if name not in reviewers.TRUSTED_REVIEWERS:
            findings = reviewers.vet_findings(findings, bundle["files_after"])
        rec.update(verdict=res["verdict"], findings=findings, checks=res.get("checks", []),
                   input_tokens=res.get("input_tokens", 0), output_tokens=res.get("output_tokens", 0),
                   model_digest=res.get("model_digest"))
    except Exception as e:  # recorded, never treated as an approval
        rec.update(verdict="error", error=f"{type(e).__name__}: {e}", findings=[], checks=[])
    rec["seconds"] = round(time.time() - t0, 2)
    rec["llm_calls"] = [{k: c.get(k) for k in ("status", "model", "model_digest", "input_tokens", "output_tokens",
                                                "runtime_seconds", "load_seconds", "eval_seconds", "error")}
                        for c in log.calls]
    return rec


def decide(reviews: list[dict], bundle: dict, run_probe, needed: int) -> dict:
    """Raw verdict vs pipeline decision for a set of reviews of one patch."""
    ok = [r for r in reviews if r.get("verdict") != "error"]
    raw = "block" if any(r["verdict"] == "request_changes" for r in ok) else "approve"
    if len(ok) < needed:
        return {"raw": raw, "pipeline": "error", "reason": "reviewer error"}
    issue = bundle["issue"]
    req = requirement_texts(issue["title"], issue["body"], bundle["prd"], bundle["files_after"], None)
    changed = {f["function"] for f in reviewers.function_inventory(bundle["files_after"])}
    agg = loop.aggregate(ok, bundle["tests"], run_probe, requirements=req, changed_functions=changed or None)
    if agg["blocking_count"]:
        pipe = "block"
    elif agg["unresolved_count"] or len(agg["inspected_reviews"]) < needed:
        pipe = "escalate"
    else:
        pipe = "approve"
    checks = agg["checks"]
    return {"raw": raw, "pipeline": pipe, "blocking": agg["blocking_count"], "unresolved": agg["unresolved_count"],
            "inspected": len(agg["inspected_reviews"]),
            "checks_total": len(checks), "checks_valid": sum(c["valid"] for c in checks),
            "check_states": {s: sum(c.get("evidence_state") == s for c in checks)
                             for s in ("confirmed", "unsupported", "conflicting", "passed", "invalid")}}


def metrics(rows: list[dict], key: str) -> dict:
    """Confusion counts for one decision mode. 'escalate' is neither a catch nor a false block."""
    pos = [r for r in rows if r["expected_blocking"]]
    neg = [r for r in rows if not r["expected_blocking"]]
    tp = sum(r[key] == "block" for r in pos)
    fa = sum(r[key] == "approve" for r in pos)     # false approvals: defect shipped
    fb = sum(r[key] == "block" for r in neg)       # false blocks: correct change rejected
    esc = sum(r[key] == "escalate" for r in rows)
    err = sum(r[key] == "error" for r in rows)
    return {"cases": len(rows), "true_positives": tp, "false_approvals": fa, "false_blocks": fb,
            "true_negatives": sum(r[key] == "approve" for r in neg), "escalations": esc, "errors": err,
            "precision": round(tp / (tp + fb), 3) if tp + fb else None,
            "recall": round(tp / len(pos), 3) if pos else None,
            "false_approval_rate": round(fa / len(pos), 3) if pos else None,
            "false_block_rate": round(fb / len(neg), 3) if neg else None}


def load_case(case_id: str) -> dict:
    case = build_case(case_id)
    case["expected_blocking"] = LABELS["cases"][case_id]["expected_blocking"]
    case["category"] = LABELS["cases"][case_id]["category"]
    return case
