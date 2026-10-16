"""Real local-LLM scenarios A-E (requires Ollama, the configured models and the Docker sandbox image).

Every model call is real; tests really execute (inside the Docker sandbox, as the real runtime requires); each
result is checked afterwards against an oracle the agents never see. Nothing is simulated or retried silently.

  A  bulk        genuine bug, real coder + two real reviewers                        (repeat with --repeat)
  B  checkout    the report names one defect; a related one must be found by review   (repeat with --repeat)
  C  bulk        feedback withheld from the coder (ablation) -> repeated solution / stall -> escalation
  D  failures    D1 Ollama unreachable, D2 model-call timeout, D3 cancellation during real coding
  E  approval    approving before a SUCCESS gate is refused; an approved workflow leaves main untouched

    python scripts/real_demo.py --scenarios A,B,C,D,E --repeat 2 --out examples/evidence/real
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ORACLES = {  # full-specification checks run after the workflow; never shown to agents
    "checkout": "from shopcart.checkout import *; assert shipping_cost(50.0) == 0.0 and shipping_cost(49.99) == 4.99 "
                "and shipping_banner(50.0) == 'Your order ships free!' and shipping_banner(45.0) == 'Add $5.00 more for free shipping'",
    "bulk": "from shopcart.cart import *; assert bulk_discount_percent(9) == 0.0 and bulk_discount_percent(10) == 5.0 "
            "and bulk_discount_percent(49) == 5.0 and bulk_discount_percent(50) == 10.0",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="A,B,C,D,E")
    ap.add_argument("--repeat", type=int, default=1, help="repetitions of A and B (repeatability)")
    ap.add_argument("--data-dir", default=str(ROOT / "data-real-demo"))
    ap.add_argument("--out", default=None, help="evidence output dir (default <data-dir>/evidence)")
    args = ap.parse_args()
    os.environ["SF_DATA_DIR"] = args.data_dir

    from systemfactory.config import Settings
    from systemfactory.demo import SAMPLES, init_demo_repo
    from systemfactory.evidence import export
    from systemfactory.execution import DockerExecutionBackend
    from systemfactory.llm import OllamaClient
    from systemfactory.orchestrator import Orchestrator, Worker
    from systemfactory.sandbox import scrubbed_env
    from systemfactory.statemachine import IllegalTransition

    s = Settings.from_env()
    st = OllamaClient(base_url=s.ollama_url).status([s.coder_model, s.reviewer_a_model, s.reviewer_b_model])
    if not st["reachable"] or not all(m.get("installed") for m in st["models"].values()):
        print("Ollama or a configured model is unavailable (scripts/check_models.py). Not substituting a simulation.")
        return 2
    ok, why = DockerExecutionBackend().available()
    if not ok:
        print(f"Docker sandbox unavailable ({why}); the real runtime requires isolation. Nothing was run.")
        return 2
    init_demo_repo(s)
    orch = Orchestrator(s)
    out_root = Path(args.out) if args.out else Path(args.data_dir) / "evidence"
    results = []

    def oracle(wf: dict, sample: str) -> bool | None:
        if not wf.get("worktree") or sample not in ORACLES:
            return None
        r = subprocess.run([sys.executable, "-c", ORACLES[sample]], cwd=wf["worktree"], capture_output=True,
                           env=scrubbed_env(extra={"PYTHONPATH": wf["worktree"]}))
        return r.returncode == 0

    def run(label: str, sample: str, options: dict | None = None, budget: dict | None = None,
            during=None) -> dict:
        smp = SAMPLES[sample]
        t0 = time.time()
        wf_id = orch.submit(smp["title"], smp["body"], source=f"sample:{sample}", runtime="ollama",
                            options=options or {}, budget=budget)
        worker = threading.Thread(target=Worker(orch, concurrency=1).run_until_idle)
        worker.start()
        if during:
            during(wf_id)
        worker.join()
        wf = orch.store.workflow(wf_id)
        its = orch.store.get_iterations(wf_id)
        calls = orch.store.get_llm_calls(wf_id)
        rec = {"scenario": label, "workflow": wf_id, "sample": sample, "state": wf["state"],
               "error": wf.get("error"), "iterations": len(its),
               "iteration_decisions": [i["decision"] for i in its],
               "quality_gate": (wf["data"].get("quality_gate") or {}).get("status"),
               "spec_satisfied_by_oracle": oracle(wf, sample), "seconds": round(time.time() - t0, 1),
               "llm_calls": len(calls), "llm_calls_failed": sum(c["status"] != "ok" for c in calls),
               "tokens_in": sum(c["input_tokens"] or 0 for c in calls),
               "tokens_out": sum(c["output_tokens"] or 0 for c in calls),
               "models": sorted({c["model"] for c in calls}),
               "backends": sorted({t["backend"] for t in orch.store.get_test_runs(wf_id)})}
        export(orch, wf_id, out_root / f"{label}-wf{wf_id}")
        rec["evidence"] = f"{label}-wf{wf_id}"  # relative to the output directory: no local paths in results
        results.append(rec)
        print(json.dumps(rec), flush=True)
        return rec

    want = {x.strip() for x in args.scenarios.split(",")}
    if "A" in want:
        for k in range(args.repeat):
            run(f"A{k + 1}-real-fix", "bulk")
    if "B" in want:
        for k in range(args.repeat):
            run(f"B{k + 1}-revision-after-review", "checkout")
    if "C" in want:
        run("C-feedback-withheld-stall", "checkout", options={"coder_feedback": False})
    if "D" in want:
        run("D1-ollama-unreachable", "bulk", options={"ollama_url": "http://127.0.0.1:9"})
        run("D2-model-timeout", "bulk", options={"llm_timeout": 1})

        def cancel_when_coding(wf_id: int) -> None:
            deadline = time.time() + 600
            while time.time() < deadline:
                if any(c["role"] == "coding" for c in orch.store.get_llm_calls(wf_id)):
                    orch.cancel(wf_id, "real-demo")
                    return
                time.sleep(0.5)
        run("D3-cancel-during-coding", "bulk", during=cancel_when_coding)
    if "E" in want:
        main_before = subprocess.run(["git", "-C", str(s.demo_repo), "rev-parse", "main"], capture_output=True,
                                     text=True).stdout.strip()
        guard = []
        for r in results:
            try:
                orch.approve(r["workflow"], "demo-reviewer", "scenario E")
                guard.append({"workflow": r["workflow"], "state_before": r["state"], "approved": True})
            except (IllegalTransition, ValueError) as e:
                guard.append({"workflow": r["workflow"], "state_before": r["state"], "approved": False,
                              "refusal": str(e)})
        main_after = subprocess.run(["git", "-C", str(s.demo_repo), "rev-parse", "main"], capture_output=True,
                                    text=True).stdout.strip()
        guard_result = {"scenario": "E-approval-guard", "attempts": guard,
             "approved_only_after_gate_success": all(
                 g["approved"] == (next(r for r in results if r["workflow"] == g["workflow"])["quality_gate"] == "SUCCESS")
                 for g in guard),
             "demo_main_unchanged": main_before == main_after, "main": main_after}
        results.append(guard_result)
        print(json.dumps(guard_result), flush=True)
        for r in results:  # re-export so the packages show the final (approved / refused) state
            if "workflow" in r and "evidence" in r:
                r["final_state"] = orch.store.workflow(r["workflow"])["state"]
                export(orch, r["workflow"], out_root / r["evidence"])
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "scenario-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
