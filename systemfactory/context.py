"""What a role sees while it runs: restricted helpers for logging, evidence, commands and budgets."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .sandbox import Cancelled, CommandResult, Git, StepTimeout, Workspace, run_command, scrubbed_env
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

    # ---- state
    @property
    def wf(self) -> dict:
        return self.store.get_workflow(self.wf_id)

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

    # ---- tools
    def workspace(self, writable: list[str]) -> Workspace:
        return Workspace(self.worktree, writable)

    def run_python(self, *args: str, timeout: float | None = None) -> CommandResult:
        """Run the project's interpreter inside the worktree with a scrubbed environment."""
        self.charge(test_runs=1)
        env = scrubbed_env(extra={"PYTHONPATH": str(self.worktree)})
        return run_command([sys.executable, *args], self.worktree,
                           timeout=timeout or self.settings.command_timeout_seconds,
                           env=env, should_stop=self.check_stop)

    def run_pytest(self, *paths: str) -> CommandResult:
        return self.run_python("-m", "pytest", "-q", "-p", "no:cacheprovider", *paths)
