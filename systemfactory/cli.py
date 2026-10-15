"""Command line interface: python -m systemfactory <command>."""
from __future__ import annotations

import argparse
import json
import sys
import time

from .config import Settings, runtime_info
from .demo import SAMPLES, init_demo_repo
from .orchestrator import Orchestrator, Worker


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "-"


def _parse_faults(items: list[str]) -> dict:
    faults = {}
    for item in items or []:
        step, _, n = item.partition("=")
        faults[step] = int(n or 1)
    return faults


def print_status(orch: Orchestrator) -> None:
    rows = orch.store.list_workflows()
    print(f"{'ID':>3}  {'STATE':<18} {'KIND':<8} {'STEP':<12} {'ATT':>3}  TITLE")
    for w in reversed(rows):
        print(f"{w['id']:>3}  {w['state']:<18} {w['kind'] or '-':<8} {w['current_step'] or '-':<12} "
              f"{w['total_attempts']:>3}  {w['issue_title'][:60]}")


def print_detail(orch: Orchestrator, wf_id: int) -> None:
    wf = orch.store.get_workflow(wf_id)
    if not wf:
        sys.exit(f"workflow {wf_id} not found")
    print(f"Workflow {wf_id}: {wf['issue_title']}\n  state={wf['state']} kind={wf['kind']} branch={wf['branch']}")
    if wf["error"]:
        print(f"  error: {wf['error']}")
    print(f"  usage: {json.dumps(wf['usage'])}")
    print("\nSteps:")
    for s in orch.store.get_steps(wf_id):
        print(f"  {s['seq']:>2}. {s['name']:<13} {s['status']:<10} attempts={s['attempts']}"
              + (f"  ({s['error'][:90]})" if s["error"] else ""))
    print("\nEvents:")
    for e in orch.store.get_events(wf_id):
        print(f"  {_fmt_time(e['ts'])} [{e['level']:<7}] {e['actor']:<14} {e['message'][:140]}")
    its = orch.store.get_iterations(wf_id)
    if its:
        print("\nImplementation-loop iterations:")
        for it in its:
            patch = it["patch"] or {}
            print(f"  #{it['n']} {it['status']:<10} blocking={it['blocking_count']} fp={(it['fingerprint'] or '-')[:10]}"
                  f"  {patch.get('mutation', '-')}  ({patch.get('strategy', '-')})")
            for r in orch.store.get_reviews(wf_id, it["n"]):
                print(f"       reviewer {r['reviewer']:<16} {r['verdict']:<16} {r['provider']}/{r['model']}"
                      f"{' [SIMULATED]' if r['simulated'] else ''}  findings={len(r['findings'])}")
            if it["decision_reason"]:
                print(f"       -> {it['decision_reason']}")
    print("\nEvidence:")
    for ev in orch.store.get_evidence(wf_id):
        print(f"  - [{ev['step']}] {ev['kind']}: {ev['title']}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="systemfactory", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("init-demo", help="create the separate buggy demo repository")
    sp.add_argument("--force", action="store_true", help="recreate demo repo and worktrees")
    sp = sub.add_parser("submit", help="submit an issue")
    sp.add_argument("--sample", choices=sorted(SAMPLES))
    sp.add_argument("--title")
    sp.add_argument("--body", default="")
    sp.add_argument("--fault", action="append", metavar="STEP=N",
                    help="inject N transient failures into STEP (demo of retries)")
    sp.add_argument("--throttle-ms", type=int, default=0, help="slow repair candidates (makes cancellation visible)")
    sp.add_argument("--coder", choices=["simulated-repair", "simulated-stubborn"], default="simulated-repair",
                    help="simulated-stubborn is a labelled test double that ignores review feedback")
    sp.add_argument("--reviewers", help="comma-separated reviewer adapters (default: settings / SF_REVIEWERS)")
    sp.add_argument("--max-iterations", type=int)
    sub.add_parser("run", help="process queued workflows until idle")
    sub.add_parser("status", help="list workflows")
    sp = sub.add_parser("show", help="show one workflow in detail")
    sp.add_argument("id", type=int)
    for name in ("approve", "reject"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
        sp.add_argument("--by", required=True, help="name of the human approver")
        sp.add_argument("--comment", default="")
    for name in ("cancel", "retry"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
    sp = sub.add_parser("serve", help="run API + dashboard + background worker")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sub.add_parser("demo", help="init demo repo, submit every sample issue, run, and summarise")
    sub.add_parser("runtime", help="show which agent runtime is active")
    args = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    settings = Settings.from_env()
    if args.cmd == "serve":
        import uvicorn
        from .api import create_app
        if args.host not in ("127.0.0.1", "localhost"):
            print("WARNING: the API has no authentication; binding beyond localhost is not recommended.")
        uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning")
        return
    if args.cmd == "init-demo":
        print(init_demo_repo(settings, force=args.force))
        return
    if args.cmd == "runtime":
        print(json.dumps(runtime_info(), indent=2))
        return

    orch = Orchestrator(settings)
    if args.cmd == "submit":
        if args.sample:
            s = SAMPLES[args.sample]
            title, body, source = s["title"], s["body"], f"sample:{args.sample}"
        elif args.title:
            title, body, source = args.title, args.body, "cli"
        else:
            sys.exit("provide --sample or --title")
        options = {"faults": _parse_faults(args.fault), "throttle_ms": args.throttle_ms, "coder": args.coder}
        if args.reviewers:
            options["reviewers"] = [r.strip() for r in args.reviewers.split(",") if r.strip()]
        budget = {"max_iterations": args.max_iterations} if args.max_iterations else None
        print(f"submitted workflow {orch.submit(title, body, source=source, options=options, budget=budget)}")
    elif args.cmd == "run":
        done = Worker(orch).run_until_idle()
        print(f"processed workflows: {done}")
        print_status(orch)
    elif args.cmd == "status":
        print_status(orch)
    elif args.cmd == "show":
        print_detail(orch, args.id)
    elif args.cmd in ("approve", "reject"):
        getattr(orch, args.cmd)(args.id, args.by, args.comment)
        print(f"workflow {args.id}: {orch.store.get_workflow(args.id)['state']}")
    elif args.cmd in ("cancel", "retry"):
        getattr(orch, args.cmd)(args.id)
        print(f"workflow {args.id}: {orch.store.get_workflow(args.id)['state']}")
    elif args.cmd == "demo":
        print(init_demo_repo(settings))
        print(f"runtime: {runtime_info()['mode']} - {runtime_info()['description']}\n")
        ids = {}
        for key, s in SAMPLES.items():
            ids[key] = orch.submit(s["title"], s["body"], source=f"sample:{key}")
        ids["fault"] = orch.submit(SAMPLES["shipping"]["title"] + " (with injected fault)",
                                   SAMPLES["shipping"]["body"], source="sample:shipping+fault",
                                   options={"faults": {"reproduce": 1}})
        ids["stubborn"] = orch.submit(SAMPLES["bulk"]["title"] + " (stubborn coder test double)",
                                      SAMPLES["bulk"]["body"], source="sample:bulk+stubborn",
                                      options={"coder": "simulated-stubborn"})
        t0 = time.time()
        Worker(orch).run_until_idle()
        print(f"ran {len(ids)} workflows in {time.time() - t0:.1f}s\n")
        print_status(orch)


if __name__ == "__main__":
    main()
