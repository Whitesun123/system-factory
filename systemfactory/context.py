"""What a role sees while it runs: restricted helpers for logging, evidence, commands and budgets."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .execution import ExecutionBackend, IsolationUnavailable, resolve_backend
from .sandbox import Cancelled, CommandResult, Git, StepTimeout, Workspace
from .store import Store


class TransientError(Exception):
    """Retryable failure (flaky tool, search tier exhausted, timeout)."""


class PermanentError(Exception):
    """Non-retryable failure: the workflow fails and keeps its evidence for a human."""


class BlockedError(Exception):
    """Needs human input before it can continue (e.g. cannot reproduce, unsupported work type)."""


@dataclass
class StepContext:
    settings: Settings
    store: Store
    git: Git
    wf_id: int
    step: str
    role: str
    attempt: int
    deadline: float
    _last_heartbeat: float = field(default=0.0)
    _backend: ExecutionBackend | None = field(default=None)

    # ---- state
    @property
    def wf(self) -> dict:
        return self.store.workflow(self.wf_id)

    @property
    def data(self) -> dict:
        return self.wf["data"]

    @property
    def worktree(self) -> Path:
        wt = self.wf.get("worktree")
        if not wt:
            raise PermanentError("workspace has not been created")
        return Path(wt)

    # ---- audit
    def log(self, message: str, level: str = "info", **data) -> None:
        self.store.log(self.wf_id, message, level=level, step=self.step, actor=self.role, data=data or None)

    def evidence(self, kind: str, title: str, content: str, **data) -> None:
        self.store.add_evidence(self.wf_id, self.step, kind, title, content, data or None)

    # ---- cooperative cancellation, deadlines, budgets
    def check_stop(self) -> None:
        now = time.time()
        if now - self._last_heartbeat > 2:
            self.store.heartbeat(self.wf_id)
            self._last_heartbeat = now
        if self.store.cancel_requested(self.wf_id):
            raise Cancelled("cancellation requested")
        if now > self.deadline:
            raise StepTimeout(f"step '{self.step}' exceeded its deadline")

    def charge(self, **deltas: float) -> None:
        usage = self.store.add_usage(self.wf_id, **deltas)
        budget = self.wf["budget"]
        for key, limit_key in (("candidate_evals", "max_candidate_evals"), ("test_runs", "max_test_runs")):
            if limit_key in budget and usage.get(key, 0) > budget[limit_key]:
                raise PermanentError(f"budget exhausted: {key}={usage[key]} > {budget[limit_key]}")
        for key, limit_key in (("llm_tokens", "max_llm_tokens"), ("cost_usd", "max_cost_usd")):
            if limit_key in budget and usage.get(key, 0) > budget[limit_key]:
                raise BlockedError(f"LLM budget exhausted: {key}={usage[key]} > {budget[limit_key]}; "
                                   "escalating to a human instead of spending more")

    # ---- implementation loop
    def iteration(self) -> dict:
        it = self.store.current_iteration(self.wf_id)
        if it is None:
            raise PermanentError("no implementation-loop iteration is open")
        return it

    def set_phase(self, phase: str) -> dict:
        it = self.iteration()
        self.store.update_iteration(self.wf_id, it["n"], status=phase)
        return self.iteration()

    # ---- tools
    def workspace(self, writable: list[str]) -> Workspace:
        return Workspace(self.worktree, writable)

    @property
    def probes_dir(self) -> Path:
        d = self.settings.data_dir / "probes" / f"wf-{self.wf_id}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def backend(self) -> ExecutionBackend:
        """The workflow's execution backend. Fails closed: never substitutes weaker isolation."""
        if self._backend is None:
            opts = self.wf["options"]
            name = opts.get("execution") or self.settings.execution
            try:
                self._backend = resolve_backend(name, bool(opts.get("require_isolation", False)))
            except IsolationUnavailable as e:
                raise BlockedError(f"{e}. Nothing was executed; start Docker / build the sandbox image, or "
                                   "explicitly choose the local backend for trusted code.") from e
        return self._backend

    def run_python(self, *args: str, timeout: float | None = None) -> CommandResult:
        """Run Python inside the worktree through the workflow's execution backend."""
        backend = self.backend
        self.charge(test_runs=1)
        return backend.run_python(list(args), workdir=self.worktree, ro_dirs={self.probes_dir: "/sf/probes"},
                                  timeout=timeout or self.settings.command_timeout_seconds,
                                  should_stop=self.check_stop)

    def run_pytest(self, *paths: str, purpose: str | None = None) -> CommandResult:
        """Run pytest and persist the run (command, commit, exit code, pass/fail counts) as test evidence."""
        res = self.run_python("-m", "pytest", "-q", "-p", "no:cacheprovider", *paths)
        counts = pytest_counts(res.output)
        try:
            commit = self.git.out(self.worktree, "rev-parse", "HEAD")
        except Exception:
            commit = None
        it = self.store.current_iteration(self.wf_id)
        self.store.add_test_run(self.wf_id, {
            "iteration": it["n"] if it else None, "step": self.step,
            "purpose": purpose or ("suite" if not paths else " ".join(paths)),
            "command": "python -m pytest -q -p no:cacheprovider " + " ".join(paths), "commit_sha": commit,
            "exit_code": res.exit_code, "timed_out": res.timed_out, "seconds": round(res.duration, 3),
            "summary": counts["summary"], "passed": counts["passed"], "failed": counts["failed"],
            "errors": counts["errors"], "output_tail": res.output[-2000:],
            "backend": f"{self.backend.name} ({self.backend.isolation})"})
        return res


def pytest_counts(output: str) -> dict:
    """Parse pytest's final summary line into counts (None when pytest printed no summary)."""
    lines = [ln.strip("= ").strip() for ln in output.strip().splitlines() if ln.strip()]
    summary = next((ln for ln in reversed(lines) if re.search(r"\d+ (passed|failed|error)", ln)), lines[-1] if lines else "")
    def n(word: str) -> int | None:
        m = re.search(rf"(\d+) {word}", summary)
        return int(m.group(1)) if m else (0 if re.search(r"\d+ (passed|failed|error)", summary) else None)
    return {"summary": summary, "passed": n("passed"), "failed": n("failed"), "errors": n("errors?")}
