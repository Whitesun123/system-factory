"""Deterministic orchestrator: drives each workflow through its pipeline.

Responsibilities: step ordering, attempts/retries with backoff, cancellation, deadlines, budgets,
fault injection for demos, human approval gates, and leasing work to concurrent workers.
Nothing in here is AI; it is plain, testable control flow.
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import Settings
from .context import BlockedError, PermanentError, StepContext, TransientError
from .roles import ROLES
from .sandbox import Cancelled, Git, StepTimeout
from .statemachine import IT, ITERATION_OPEN, LOOP_STEPS, PIPELINES, STEP, TERMINAL, WF, IllegalTransition
from .store import Store


class Orchestrator:
    def __init__(self, settings: Settings, store: Store | None = None):
        settings.ensure_dirs()
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.git = Git(settings.empty_gitconfig, timeout=settings.command_timeout_seconds)

    # ------------------------------------------------------------------ intake
    def submit(self, title: str, body: str, *, repo: Path | str | None = None, source: str = "manual",
               options: dict | None = None, budget: dict | None = None) -> int:
        repo = Path(repo or self.settings.demo_repo)
        if not (repo / ".git").exists():
            raise ValueError(f"target repository not initialised: {repo} (run `python -m systemfactory init-demo`)")
        if not title.strip():
            raise ValueError("issue title is required")
        wf_id = self.store.create_workflow(title.strip(), body, str(repo), source=source, options=options or {},
                                           budget={**self.settings.default_budget, **(budget or {})})
        self.store.add_steps(wf_id, [PIPELINES["bug"][0]])  # triage decides the rest
        return wf_id

    # ------------------------------------------------------------------ execution
    def run_workflow(self, wf_id: int) -> str:
        """Run a claimed (RUNNING) workflow until it waits, blocks, fails, is cancelled or completes."""
        try:
            return self._drive(wf_id)
        except Exception as e:  # orchestrator bug: never leave a workflow stuck in RUNNING
            self.store.log(wf_id, f"Orchestrator error: {e}", level="error", data={"traceback": traceback.format_exc()})
            try:
                self.store.transition(wf_id, WF.FAILED, reason=f"orchestrator error: {e}", error=str(e), lease_owner=None)
            except IllegalTransition:
                pass
            return WF.FAILED

    def _drive(self, wf_id: int) -> str:
        while True:
            wf = self.store.get_workflow(wf_id)
            if wf["state"] != WF.RUNNING:
                return wf["state"]
            if wf["cancel_requested"]:
                return self._finish_cancel(wf_id)
            max_wall = wf["budget"].get("max_wall_seconds")
            if max_wall and wf["started_at"] and time.time() - wf["started_at"] > max_wall:
                self.store.transition(wf_id, WF.FAILED, reason=f"wall-clock budget of {max_wall}s exhausted",
                                      error="wall-clock budget exhausted", lease_owner=None)
                return WF.FAILED
            step = next((s for s in self.store.get_steps(wf_id) if s["status"] == STEP.PENDING), None)
            if step is None:
                self.store.transition(wf_id, WF.FAILED, reason="pipeline ended without an approval gate",
                                      error="no approval step", lease_owner=None)
                return WF.FAILED
            if step["name"] == "approval":
                self.store.update_step(step["id"], status=STEP.WAITING, started_at=time.time())
                self.store.transition(wf_id, WF.AWAITING_APPROVAL, current_step="approval", lease_owner=None,
                                      reason="all automated checks done; waiting for a human decision")
                return WF.AWAITING_APPROVAL
            outcome = self._run_step(wf, step)
            if outcome != STEP.SUCCEEDED:
                return self.store.get_workflow(wf_id)["state"]
            if step["name"] == "triage":
                kind = self.store.get_workflow(wf_id)["kind"]
                self.store.add_steps(wf_id, PIPELINES[kind][1:])
                self.store.log(wf_id, f"Pipeline for {kind}: " + " -> ".join(n for n, _ in PIPELINES[kind]))
            if step["name"] == "feedback":
                decision = self.store.get_step(wf_id, "feedback")["output"].get("decision")
                if decision == "revise":
                    self._loop_back(wf_id)

    def _loop_back(self, wf_id: int) -> None:
        """Re-arm the implementation-loop steps so the next iteration starts at coding."""
        it = self.store.current_iteration(wf_id)
        for s in self.store.get_steps(wf_id):
            if s["name"] in LOOP_STEPS:
                self.store.update_step(s["id"], status=STEP.PENDING, attempts=0, error=None,
                                       started_at=None, finished_at=None)
        items = len((it.get("feedback") or {}).get("items", []))
        self.store.log(wf_id, f"Revision requested: iteration {it['n']} -> {it['n'] + 1}; {items} feedback item(s) "
                              f"passed to the coding agent", step="feedback")

    def _attempt_iteration(self, wf_id: int, name: str) -> int | None:
        if name not in LOOP_STEPS:
            return None
        it = self.store.current_iteration(wf_id)
        if it and it["status"] in ITERATION_OPEN:
            return it["n"]
        if name == "coding":
            return (it["n"] + 1) if it else 1
        return it["n"] if it else None

    def _run_step(self, wf: dict, step: dict) -> str:
        wf_id, name, role = wf["id"], step["name"], step["role"]
        max_attempts = self.settings.max_attempts_for(name)
        first = step["attempts"] + 1
        injected = int(wf["options"].get("faults", {}).get(name, 0))
        self.store.update_step(step["id"], status=STEP.RUNNING, started_at=time.time(), error=None)
        self.store.update_workflow(wf_id, current_step=name)
        self.store.log(wf_id, f"Step '{name}' started (role: {role}, up to {max_attempts} attempts)", step=name)
        last_error = ""
        for n in range(first, first + max_attempts):
            att = self.store.start_attempt(wf_id, step["id"], n, self._attempt_iteration(wf_id, name))
            t0 = time.time()
            ctx = StepContext(self.settings, self.store, self.git, wf_id, name, role, attempt=n,
                              deadline=t0 + self.settings.timeout_for(name))
            try:
                ctx.check_stop()
                if n <= injected:
                    raise TransientError(f"INJECTED transient fault on attempt {n} (demo fault injection)")
                output = ROLES[name](ctx)
            except Cancelled:
                self.store.finish_attempt(att, "cancelled", "cancellation requested", {"seconds": time.time() - t0})
                self.store.update_step(step["id"], status=STEP.CANCELLED, finished_at=time.time())
                self._finish_cancel(wf_id)
                return STEP.CANCELLED
            except (TransientError, StepTimeout) as e:
                last_error = str(e)
                self.store.finish_attempt(att, "failed", last_error, {"retryable": True, "seconds": time.time() - t0})
                self.store.log(wf_id, f"Attempt {n} failed (retryable): {e}", level="warning", step=name)
                if n < first + max_attempts - 1:
                    if not self._backoff(ctx, n - first):
                        self.store.update_step(step["id"], status=STEP.CANCELLED, finished_at=time.time())
                        self._finish_cancel(wf_id)
                        return STEP.CANCELLED
                    self.store.log(wf_id, f"Retrying '{name}' (attempt {n + 1})", step=name)
                continue
            except BlockedError as e:
                self.store.finish_attempt(att, "blocked", str(e), {"seconds": time.time() - t0})
                self.store.update_step(step["id"], status=STEP.BLOCKED, error=str(e), finished_at=time.time())
                self._close_iteration(wf_id, IT.EXHAUSTED if "budget" in str(e).lower() else IT.STALLED, str(e))
                self.store.transition(wf_id, WF.BLOCKED, reason=str(e), error=str(e), lease_owner=None)
                return STEP.BLOCKED
            except Exception as e:  # PermanentError, GitError, PermissionError, unexpected
                kind = "permanent" if isinstance(e, PermanentError) else type(e).__name__
                self.store.finish_attempt(att, "failed", str(e), {"retryable": False, "error_type": kind,
                                                                  "seconds": time.time() - t0})
                self.store.log(wf_id, f"Attempt {n} failed ({kind}): {e}", level="error", step=name,
                               data={"traceback": traceback.format_exc()[-3000:]})
                self.store.update_step(step["id"], status=STEP.FAILED, error=str(e), finished_at=time.time())
                self._close_iteration(wf_id, IT.FAILED, str(e))
                self.store.transition(wf_id, WF.FAILED, reason=f"{name}: {e}", error=str(e), lease_owner=None)
                return STEP.FAILED
            else:
                self.store.finish_attempt(att, "succeeded", None, {"seconds": round(time.time() - t0, 3)})
                self.store.update_step(step["id"], status=STEP.SUCCEEDED, output=output, finished_at=time.time())
                self.store.merge_data(wf_id, name, output)
                self.store.log(wf_id, f"Step '{name}' succeeded on attempt {n} in {time.time() - t0:.2f}s", step=name)
                return STEP.SUCCEEDED
        msg = f"{name}: retries exhausted after {max_attempts} attempts; last error: {last_error}"
        self.store.update_step(step["id"], status=STEP.FAILED, error=msg, finished_at=time.time())
        self._close_iteration(wf_id, IT.FAILED, msg)
        self.store.transition(wf_id, WF.FAILED, reason=msg, error=msg, lease_owner=None)
        return STEP.FAILED

    def _backoff(self, ctx: StepContext, k: int) -> bool:
        """Exponential backoff that stays responsive to cancellation. Returns False if cancelled."""
        end = time.time() + min(0.25 * 2 ** k, 2.0)
        while time.time() < end:
            if self.store.cancel_requested(ctx.wf_id):
                return False
            time.sleep(0.05)
        return True

    def _close_iteration(self, wf_id: int, status: str, reason: str) -> None:
        """Close the open loop iteration (if any) so its history shows how it ended."""
        try:
            self.store.close_open_iteration(wf_id, status, reason)
        except IllegalTransition:
            self.store.close_open_iteration(wf_id, IT.FAILED, reason)

    def _finish_cancel(self, wf_id: int, actor: str = "orchestrator") -> str:
        self._close_iteration(wf_id, IT.CANCELLED, "cancelled by request")
        for s in self.store.get_steps(wf_id):
            if s["status"] in (STEP.PENDING, STEP.RUNNING, STEP.WAITING):
                self.store.update_step(s["id"], status=STEP.CANCELLED, finished_at=time.time())
        try:
            self.store.transition(wf_id, WF.CANCELLED, actor=actor, reason="cancelled by request", lease_owner=None)
        except IllegalTransition:
            pass
        return WF.CANCELLED

    # ------------------------------------------------------------------ human actions
    def cancel(self, wf_id: int, actor: str = "human") -> None:
        wf = self._get(wf_id)
        if wf["state"] in TERMINAL:
            raise IllegalTransition(f"workflow {wf_id} is already {wf['state']}")
        self.store.request_cancel(wf_id)
        self.store.log(wf_id, f"Cancellation requested by {actor}", actor=f"human:{actor}")
        if wf["state"] != WF.RUNNING:  # nobody is executing it: cancel directly
            self._finish_cancel(wf_id, actor=f"human:{actor}")
        # RUNNING: the worker observes the flag at its next check (between candidates / while polling commands)

    def approve(self, wf_id: int, actor: str, comment: str = "") -> None:
        self._decide(wf_id, "approved", actor, comment)

    def reject(self, wf_id: int, actor: str, comment: str = "") -> None:
        self._decide(wf_id, "rejected", actor, comment)

    def _decide(self, wf_id: int, decision: str, actor: str, comment: str) -> None:
        if not actor or not actor.strip():
            raise ValueError("approver name is required")
        wf = self._get(wf_id)
        if wf["state"] != WF.AWAITING_APPROVAL:
            raise IllegalTransition(f"workflow {wf_id} is {wf['state']}, not awaiting approval")
        if (decision == "approved" and wf["kind"] in ("bug", "feature")
                and wf["data"].get("quality_gate", {}).get("status") != "SUCCESS"):
            raise IllegalTransition(f"workflow {wf_id} has not passed the quality gate; it cannot be approved")
        self.store.record_approval(wf_id, decision, actor.strip(), comment)
        step = self.store.get_step(wf_id, "approval")
        self.store.update_step(step["id"], status=STEP.SUCCEEDED, finished_at=time.time(),
                               output={"decision": decision, "actor": actor.strip(), "comment": comment})
        self.store.transition(wf_id, WF.APPROVED if decision == "approved" else WF.REJECTED,
                              actor=f"human:{actor.strip()}", reason=comment or decision)
        if decision == "approved" and wf.get("branch"):
            self.store.log(wf_id, f"Branch {wf['branch']} is ready for a human to merge. System Factory does not "
                                  "merge, push or deploy.", actor="orchestrator")

    def retry(self, wf_id: int, actor: str = "human") -> None:
        wf = self._get(wf_id)
        if wf["state"] not in (WF.FAILED, WF.BLOCKED):
            raise IllegalTransition(f"only failed or blocked workflows can be retried (state: {wf['state']})")
        steps = self.store.get_steps(wf_id)
        stuck = {s["name"] for s in steps if s["status"] in (STEP.FAILED, STEP.BLOCKED)}
        if stuck & set(LOOP_STEPS) and self.store.current_iteration(wf_id):
            # a stalled/exhausted loop restarts at coding with one more iteration of budget
            for s in steps:
                if s["name"] in LOOP_STEPS:
                    self.store.update_step(s["id"], status=STEP.PENDING, attempts=0, error=None)
            budget = wf["budget"]
            done = len(self.store.get_iterations(wf_id))
            if done >= budget.get("max_iterations", 4):
                budget["max_iterations"] = done + 1
                self.store.update_workflow(wf_id, budget=budget)
            self.store.log(wf_id, f"Human retry: implementation loop restarts at coding (iteration {done + 1}, "
                                  f"max_iterations={budget.get('max_iterations')})", actor=f"human:{actor}")
        else:
            for s in steps:
                if s["status"] in (STEP.FAILED, STEP.BLOCKED):
                    self.store.update_step(s["id"], status=STEP.PENDING, error=None)
        self.store.update_workflow(wf_id, error=None, cancel_requested=0)
        self.store.transition(wf_id, WF.QUEUED, actor=f"human:{actor}", reason="manual retry")

    def _get(self, wf_id: int) -> dict:
        wf = self.store.get_workflow(wf_id)
        if wf is None:
            raise KeyError(f"workflow {wf_id} not found")
        return wf


class Worker:
    """Claims queued workflows and runs them, at most `concurrency` at a time."""

    def __init__(self, orch: Orchestrator, concurrency: int | None = None):
        self.orch = orch
        self.concurrency = concurrency or orch.settings.concurrency
        self.owner = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._slots = threading.Semaphore(self.concurrency)
        self._pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="sf-wf")
        self.active: set[int] = set()

    def run_until_idle(self) -> list[int]:
        """Synchronously process the queue (used by the CLI and tests)."""
        done = []
        while (wf_id := self.orch.store.claim_next(self.owner)) is not None:
            self.orch.run_workflow(wf_id)
            done.append(wf_id)
        return done

    def start(self) -> None:
        recovered = self.orch.store.recover_stale(stale_seconds=30)
        if recovered:
            print(f"[worker] recovered stale workflows: {recovered}")
        self._thread = threading.Thread(target=self._loop, name="sf-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._slots.acquire(timeout=0.5):
                continue
            wf_id = self.orch.store.claim_next(self.owner)
            if wf_id is None:
                self._slots.release()
                self._stop.wait(0.5)
                continue
            self.active.add(wf_id)
            self._pool.submit(self._run, wf_id)

    def _run(self, wf_id: int) -> None:
        try:
            self.orch.run_workflow(wf_id)
        finally:
            self.active.discard(wf_id)
            self._slots.release()
