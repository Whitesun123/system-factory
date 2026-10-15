"""Run the reviewer evaluation with a hard inference budget, checkpointing every model call.

    python -m evaluation.run --configs ollama-a=phi4:14b,ollama-b=qwen3:14b --budget-minutes 30

Results are cached in evaluation/results/cache.jsonl, keyed by case content, reviewer role, model, model digest and
prompt hash. Re-running reuses cached reviews (no inference). When the budget is spent, the remaining (case, config)
pairs are reported as not executed; nothing is extrapolated.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation import harness  # noqa: E402
from evaluation.dataset import CASE_IDS  # noqa: E402
from systemfactory.config import Settings  # noqa: E402
from systemfactory.llm import OllamaClient  # noqa: E402

RESULTS = ROOT / "evaluation" / "results"


def vram_mib() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
        return int(out[0]) if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def load_cache(path: Path) -> dict:
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                cache[rec["key"]] = rec
    return cache


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", required=True, help="comma-separated role=model, role in ollama-a/ollama-b")
    ap.add_argument("--budget-minutes", type=float, default=30.0, help="cumulative NEW inference wall time")
    ap.add_argument("--cases", help="comma-separated case ids (default: all)")
    ap.add_argument("--execution", default="local", choices=["local", "docker"], help="where probes/tests run")
    args = ap.parse_args()
    configs = [tuple(c.split("=", 1)) for c in args.configs.split(",")]
    case_ids = args.cases.split(",") if args.cases else CASE_IDS
    RESULTS.mkdir(parents=True, exist_ok=True)
    cache_path = RESULTS / "cache.jsonl"
    cache = load_cache(cache_path)
    client = OllamaClient()
    status = client.status(sorted({m for _, m in configs}))
    if not status["reachable"]:
        print(f"Ollama unavailable: {status.get('error')}. No results produced (no simulated substitute).")
        return 2
    digests = {m: status["models"].get(m, {}).get("digest") for _, m in configs}
    missing = [m for m, d in digests.items() if not d]
    if missing:
        print(f"models not installed: {missing}")
        return 2
    budget_s, spent, peak_vram = args.budget_minutes * 60, 0.0, vram_mib() or 0
    settings = Settings(data_dir=Path(tempfile.mkdtemp(prefix="sf-eval-")))
    settings.ensure_dirs()
    rows, skipped = [], []
    t_start = time.time()
    for cid in case_ids:
        case = harness.load_case(cid)
        with tempfile.TemporaryDirectory() as td:
            m = harness.Materialised(case, Path(td), settings.empty_gitconfig, args.execution)
            m.apply_patch()
            bundle = m.bundle()
            per = {"case": cid, "category": case["category"], "expected_blocking": case["expected_blocking"],
                   "reviews": {}}
            for name in ("simulated-spec", "simulated-diff"):  # deterministic baseline, no inference
                per["reviews"][name] = harness.run_reviewer(name, None, bundle, m)
            for role, model in configs:
                key = harness.sha(json.dumps([harness.case_hash(case), role, model, digests[model],
                                              harness.prompt_hash(role)]))
                label = f"{role}={model}"
                if key in cache:
                    per["reviews"][label] = cache[key]["review"]
                    continue
                if spent >= budget_s:
                    skipped.append({"case": cid, "config": label})
                    continue
                t0 = time.time()
                rev = harness.run_reviewer(role, model, bundle, m)
                spent += time.time() - t0
                peak_vram = max(peak_vram, vram_mib() or 0)
                rec = {"key": key, "case": cid, "role": role, "model": model, "digest": digests[model],
                       "prompt_hash": harness.prompt_hash(role), "recorded_at": time.time(), "review": rev}
                with open(cache_path, "a", encoding="utf-8") as f:  # checkpoint immediately
                    f.write(json.dumps(rec) + "\n")
                cache[key] = rec
                per["reviews"][label] = rev
                print(f"{cid:<30} {label:<26} verdict={rev['verdict']:<16} {rev['seconds']:>6.1f}s "
                      f"spent={spent / 60:.1f}/{args.budget_minutes:.0f}min", flush=True)
            # decisions: each reviewer alone, deterministic panel, and every A x B model panel
            per["decisions"] = {}
            for label, rev in per["reviews"].items():
                per["decisions"][label] = harness.decide([rev], bundle, m.run_probe, needed=1)
            per["decisions"]["panel:simulated-spec+simulated-diff"] = harness.decide(
                [per["reviews"]["simulated-spec"], per["reviews"]["simulated-diff"]], bundle, m.run_probe, needed=2)
            a_cfgs = [f"{r}={mm}" for r, mm in configs if r == "ollama-a"]
            b_cfgs = [f"{r}={mm}" for r, mm in configs if r == "ollama-b"]
            for a in a_cfgs:
                for b in b_cfgs:
                    if a in per["reviews"] and b in per["reviews"]:
                        per["decisions"][f"panel:{a}+{b}"] = harness.decide(
                            [per["reviews"][a], per["reviews"][b]], bundle, m.run_probe, needed=2)
            rows.append(per)

    summary = {}
    labels = sorted({d for r in rows for d in r["decisions"]})
    for label in labels:
        rs = [{"expected_blocking": r["expected_blocking"], **r["decisions"][label]} for r in rows if label in r["decisions"]]
        revs = [r["reviews"][label] for r in rows if label in r["reviews"]]
        entry: dict[str, Any] = {"raw": harness.metrics(rs, "raw"), "pipeline": harness.metrics(rs, "pipeline")}
        if revs:
            secs = sorted(rv["seconds"] for rv in revs)
            entry["latency_s"] = {"mean": round(sum(secs) / len(secs), 2), "p95": secs[int(0.95 * (len(secs) - 1))]}
            entry["tokens"] = {"input_mean": round(sum(rv.get("input_tokens", 0) for rv in revs) / len(revs)),
                               "output_mean": round(sum(rv.get("output_tokens", 0) for rv in revs) / len(revs))}
            tot = sum(r.get("checks_total", 0) for r in rs)
            entry["check_validity_rate"] = round(sum(r.get("checks_valid", 0) for r in rs) / tot, 3) if tot else None
            entry["errors"] = sum(rv["verdict"] == "error" for rv in revs)
        summary[label] = entry
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "dataset_cases": len(case_ids),
              "executed_cases": len(rows), "not_executed": skipped, "configs": [f"{r}={mm}" for r, mm in configs],
              "model_digests": digests, "prompt_hashes": {r: harness.prompt_hash(r) for r, _ in configs},
              "ollama_version": status.get("version"), "platform": platform.platform(),
              "gpu_peak_vram_mib": peak_vram, "new_inference_minutes": round(spent / 60, 2),
              "budget_minutes": args.budget_minutes, "wall_minutes": round((time.time() - t_start) / 60, 2),
              "execution_backend": args.execution, "summary": summary, "cases": rows}
    (RESULTS / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nexecuted {len(rows)} cases; {len(skipped)} (case, config) pairs not executed (budget); "
          f"new inference {spent / 60:.1f} min; report: {RESULTS / 'report.json'}")
    for label, e in summary.items():
        p = e["pipeline"]
        print(f"{label:<60} raw FA={e['raw']['false_approvals']} FB={e['raw']['false_blocks']} | pipeline "
              f"TP={p['true_positives']} FA={p['false_approvals']} FB={p['false_blocks']} ESC={p['escalations']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
