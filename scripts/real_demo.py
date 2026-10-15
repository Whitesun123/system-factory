"""Real local-LLM demonstration (requires Ollama + the configured models; NOT run by the unit tests).

Runs the scenarios against a fresh data directory, then checks each result against an oracle the agents
never see, and writes a sanitized evidence package per workflow:

  A  checkout / bulk      real coder + real reviewers on a genuine bug
  B  checkout             a related defect is not mentioned in the report; reviewers must find it
  C  bulk --no-feedback   ablation: coder never sees review feedback -> repeated-solution / stall detection
  D  unreachable URL      provider unavailable -> retries, then failed with the real error recorded
  E  approval guard       approving a blocked/failed workflow is refused; nothing is ever merged

    python scripts/real_demo.py [--scenarios A,B,C,D,E] [--data-dir ./data-real-demo]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ORACLES = {  # independent spec checks, executed after the run; agents never see these
    "checkout": "from shopcart.checkout import *; assert shipping_cost(50.0) == 0.0 and shipping_cost(49.99) == 4.99 "
                "and shipping_banner(50.0) == 'Your order ships free!' and shipping_banner(45.0) == 'Add $5.00 more for free shipping'",
    "bulk": "from shopcart.cart import *; assert bulk_discount_percent(9) == 0.0 and bulk_discount_percent(10) == 5.0 "
            "and bulk_discount_percent(50) == 10.0",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="A,B,C,D,E")
    ap.add_argument("--data-dir", default=str(ROOT / "data-real-demo"))
    args = ap.parse_args()
    os.environ["SF_DATA_DIR"] = args.data_dir

    from systemfactory.config import Settings
    from systemfactory.demo import SAMPLES, init_demo_repo
    from systemfactory.evidence import export
    from systemfactory.llm import OllamaClient
    from systemfactory.orchestrator import Orchestrator, Worker
    from systemfactory.statemachine import IllegalTransition

    s = Settings.from_env()
    st = OllamaClient(base_url=s.ollama_url).status([s.coder_model, s.reviewer_a_model, s.reviewer_b_model])
    if not st["reachable"] or not all(m.get("installed") for m in st["models"].values()):
        print("Ollama or a configured model is unavailable; run scripts/check_models.py. Not substituting a simulation.")
        return 2
    init_demo_repo(s)
    orch = Orchestrator(s)
    plan = {"A": ("bulk", {}, None), "B": ("checkout", {}, None),
            "C": ("bulk", {"coder_feedback": False}, None),
            "D": ("checkout", {"ollama_url": "http://127.0.0.1:9"}, None)}
    results = []
    for key in [k.strip() for k in args.scenarios.split(",")]:
        if key not in plan:
            continue
        sample, extra, budget = plan[key]
        smp = SAMPLES[sample]
        t0 = time.time()
        wf_id = orch.submit(smp["title"], smp["body"], source=f"sample:{sample}", runtime="ollama", options=extra,
                            budget=budget)
        Worker(orch, concurrency=1).run_until_idle()
        wf = orch.store.get_workflow(wf_id)
        spec_ok = None
        if wf.get("worktree") and sample in ORACLES:
            r = subprocess.run([sys.executable, "-c", ORACLES[sample]], cwd=wf["worktree"],
                               env={**os.environ, "PYTHONPATH": wf["worktree"]}, capture_output=True)
            spec_ok = r.returncode == 0
        out = export(orch, wf_id)
        results.append({"scenario": key, "workflow": wf_id, "state": wf["state"], "error": wf.get("error"),
                        "iterations": len(orch.store.get_iterations(wf_id)), "seconds": round(time.time() - t0, 1),
                        "llm_tokens": wf["usage"].get("llm_tokens"), "spec_satisfied": spec_ok, "evidence": str(out)})
        print(json.dumps(results[-1]), flush=True)
    if "E" in args.scenarios:
        refused = 0
        for r in results:
            try:
                orch.approve(r["workflow"], "demo")
            except (IllegalTransition, ValueError):
                refused += 1
        print(json.dumps({"scenario": "E", "approval_attempts": len(results), "refused": refused,
                          "note": "only workflows with quality gate SUCCESS can be approved; approval never merges"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
