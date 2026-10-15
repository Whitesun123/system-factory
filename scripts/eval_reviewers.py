"""Benchmark reviewer models on labelled patches before choosing them.

Each case applies a known patch to a fresh copy of the demo repository, runs the real tests, and asks a
reviewer for a review. The score uses the *effective* outcome after System Factory's evidence rules
(probes re-executed, unverified blockers downgraded): a case is correct when the effective blocking
count is > 0 exactly for patches that are known to be incomplete.

Labels are established by construction and checked by an oracle (documented examples and boundary checks)
that reviewers never see.

Usage:  python scripts/eval_reviewers.py --models phi4:14b,gemma3:12b --reviewer ollama-a
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from systemfactory import loop, reviewers  # noqa: E402
from systemfactory.config import DEMO_TEMPLATE  # noqa: E402
from systemfactory.demo import SAMPLES  # noqa: E402
from systemfactory.llm import Recorder  # noqa: E402
from systemfactory.roles import REPRO_TEMPLATE, extract_example  # noqa: E402

CASES = [
    # (name, sample, file, [(old, new)], expected_blocking, oracle)
    ("checkout-incomplete", "checkout", "shopcart/checkout.py",
     [("if subtotal > FREE_SHIPPING_THRESHOLD:", "if subtotal >= FREE_SHIPPING_THRESHOLD:")], True,
     "from shopcart.checkout import *; assert shipping_cost(50.0) == 0.0 and shipping_banner(50.0) == 'Your order ships free!'"),
    ("checkout-complete", "checkout", "shopcart/checkout.py",
     [("if subtotal > FREE_SHIPPING_THRESHOLD:", "if subtotal >= FREE_SHIPPING_THRESHOLD:"),
      ("if remaining < 0:", "if remaining <= 0:")], False,
     "from shopcart.checkout import *; assert shipping_cost(50.0) == 0.0 and shipping_banner(50.0) == 'Your order ships free!'"),
    ("bulk-incomplete", "bulk", "shopcart/cart.py",
     [("if quantity > 10:", "if quantity >= 10:")], True,
     "from shopcart.cart import *; assert bulk_discount_percent(10) == 5.0 and bulk_discount_percent(50) == 10.0"),
    ("bulk-complete", "bulk", "shopcart/cart.py",
     [("if quantity > 50:", "if quantity >= 50:"), ("if quantity > 10:", "if quantity >= 10:")], False,
     "from shopcart.cart import *; assert bulk_discount_percent(10) == 5.0 and bulk_discount_percent(50) == 10.0"),
    ("pagination-correct", "pagination", "shopcart/pagination.py",
     [("items[start:start + page_size - 1]", "items[start:start + page_size]")], False,
     "from shopcart.pagination import *; assert paginate(list(range(10)), 1, 3) == [0, 1, 2]"),
]

CRITERIA = ["A reproduction test derived from the issue example fails on the base commit",
            "The same test passes after the change", "The full existing test suite still passes",
            "Only the source files named in the plan are modified; tests are not modified by the coding agent",
            "No security findings of high severity in the diff",
            "A human approves the change before it is merged (System Factory never merges)"]


def sh(cwd, *args, env=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", env=env)


def build_case(tmp: Path, sample: str, file: str, edits: list) -> tuple[Path, dict]:
    repo = tmp / "repo"
    shutil.copytree(DEMO_TEMPLATE, repo, ignore=shutil.ignore_patterns("__pycache__"))
    g = ["git", "-c", "user.name=eval", "-c", "user.email=eval@localhost", "-c", "core.autocrlf=false"]
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, *g, "add", "-A"); sh(repo, *g, "commit", "-qm", "base")
    s = SAMPLES[sample]
    (repo / "tests" / "test_issue_1.py").write_text(
        REPRO_TEMPLATE.format(issue_id=1, title=s["title"], example=extract_example(s["body"])), encoding="utf-8")
    sh(repo, *g, "add", "-A"); sh(repo, *g, "commit", "-qm", "repro")
    checkpoint = sh(repo, "git", "rev-parse", "HEAD").stdout.strip()
    p = repo / file
    text = p.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8", newline="\n")
    sh(repo, *g, "commit", "-qam", "patch")
    env = {**__import__("os").environ, "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    repro = sh(repo, sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_issue_1.py", env=env)
    suite = sh(repo, sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", env=env)
    tail = lambda r: r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    bundle = {
        "iteration": 1, "issue": {"title": s["title"], "body": s["body"]},
        "prd": {"title": s["title"], "type": "bug", "acceptance_criteria": CRITERIA},
        "plan": {"target_files": [file], "steps": []},
        "diff": sh(repo, "git", "diff", f"{checkpoint}..HEAD", "--", file).stdout,
        "diff_u0": sh(repo, "git", "diff", "-U0", f"{checkpoint}..HEAD", "--", file).stdout,
        "changed_files": [file], "files_after": {file: text},
        "tests": {"repro_passed": repro.returncode == 0, "suite_passed": suite.returncode == 0,
                  "repro_summary": tail(repro), "suite_summary": tail(suite), "probes": [], "probes_passed": True,
                  "security": {"findings": []}},
        "fingerprint": None, "patch": {}, "previous_iterations": [],
    }
    return repo, bundle


class Tools:
    def __init__(self, repo: Path):
        self.repo = repo

    def run_probe(self, source: str):
        f = self.repo.parent / f"probe-{loop.probe_id(source)}.txt"
        f.write_text(source, encoding="utf-8")
        r = sh(self.repo, sys.executable, "-c", loop.PROBE_RUNNER, str(f),
               env={**__import__("os").environ, "PYTHONPATH": str(self.repo)})
        return r.returncode == 0, r.stdout + r.stderr


class _NullStore:
    """Recorder target for the benchmark: keeps call records in memory instead of a workflow database."""
    def __init__(self):
        self.calls = []

    def add_llm_call(self, rec):
        self.calls.append(rec)
        return len(self.calls)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--reviewer", default="ollama-a", choices=["ollama-a", "ollama-b"])
    ap.add_argument("--out", default=str(ROOT / "examples" / "reviewer-benchmark"))
    args = ap.parse_args()
    results = []
    for model in args.models.split(","):
        for name, sample, file, edits, expected, oracle in CASES:
            with tempfile.TemporaryDirectory() as td:
                repo, bundle = build_case(Path(td), sample, file, edits)
                truth = sh(repo, sys.executable, "-c", oracle, env={**__import__("os").environ, "PYTHONPATH": str(repo)})
                assert (truth.returncode != 0) == expected, f"label/oracle mismatch for {name}"
                r = reviewers.OllamaReviewer(args.reviewer, model)
                store = _NullStore()
                r.recorder = Recorder(store, 0, 1, "review", r.name)
                t0 = time.time()
                try:
                    out = r.review(bundle, Tools(repo))
                    rev = {"reviewer": r.name, "verdict": out["verdict"], "simulated": False, "findings": out["findings"], "checks": out["checks"]}
                    agg = loop.aggregate([rev], bundle["tests"], Tools(repo).run_probe)
                    eff = agg["blocking_count"] > 0
                    row = {"model": model, "case": name, "expected_blocking": expected, "raw_verdict": out["verdict"],
                           "raw_blocking": sum(f["severity"] == "blocking" for f in out["findings"]),
                           "effective_blocking": agg["blocking_count"], "correct": eff == expected,
                           "probe_outcomes": [f.get("probe_outcome") for i in agg["items"] for f in i["findings"]],
                           "checks": [(c["call"], c["expected"], c["outcome"], c.get("got")) for c in agg["checks"]],
                           "seconds": round(time.time() - t0, 1), "output_tokens": out["output_tokens"],
                           "findings": [(f["severity"], f["title"]) for f in out["findings"]]}
                except Exception as e:  # report failures, never hide them
                    row = {"model": model, "case": name, "expected_blocking": expected, "error": f"{type(e).__name__}: {e}",
                           "correct": False, "seconds": round(time.time() - t0, 1)}
                results.append(row)
                print(json.dumps({k: row[k] for k in row if k != "findings"}), flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{args.reviewer}-{int(time.time())}.json"
    path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\nscore per model:")
    for model in args.models.split(","):
        rows = [r for r in results if r["model"] == model]
        fa = sum(1 for r in rows if r["expected_blocking"] and not r["correct"])
        fb = sum(1 for r in rows if not r["expected_blocking"] and not r["correct"])
        print(f"  {model:<20} {sum(r['correct'] for r in rows)}/{len(rows)} correct  "
              f"false-approve={fa} false-block={fb}  {sum(r['seconds'] for r in rows):.0f}s")
    print(f"written: {path}")


if __name__ == "__main__":
    main()
