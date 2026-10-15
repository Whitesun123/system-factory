"""Paths and runtime settings. Everything lives under a single local data directory."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_TEMPLATE = PROJECT_ROOT / "demo" / "shopcart_template"


@dataclass
class Settings:
    data_dir: Path
    # Default per-workflow budget. Every unit here is something we actually count.
    default_budget: dict = field(default_factory=lambda: {
        "max_wall_seconds": 900,
        "max_candidate_evals": 400,
        "max_test_runs": 800,
        # implementation loop (coding -> tests -> reviewers -> feedback -> revision)
        "max_iterations": 4,
        "max_loop_seconds": 600,
        "max_llm_tokens": 200_000,
        "max_cost_usd": 1.00,
        "stall_window": 2,          # consecutive iterations without fewer blocking findings
    })
    max_attempts: dict = field(default_factory=lambda: {"coding": 3, "review": 2, "default": 2})
    step_timeout_seconds: dict = field(default_factory=lambda: {"coding": 240, "review": 300, "default": 120})
    # Reviewer adapters, in order. Overridable per workflow (options.reviewers) or via SF_REVIEWERS.
    reviewers: list = field(default_factory=lambda: [
        r.strip() for r in os.environ.get("SF_REVIEWERS", "simulated-spec,simulated-diff").split(",") if r.strip()])
    min_reviewers: int = 2
    command_timeout_seconds: int = 60
    concurrency: int = 2

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(data_dir=Path(os.environ.get("SF_DATA_DIR", PROJECT_ROOT / "data")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "systemfactory.db"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def demo_repo(self) -> Path:
        return self.repos_dir / "shopcart"

    @property
    def empty_gitconfig(self) -> Path:
        """Points GIT_CONFIG_GLOBAL here so the user's global git config never affects agents."""
        return self.data_dir / "gitconfig-empty"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.repos_dir, self.worktrees_dir):
            d.mkdir(parents=True, exist_ok=True)
        if not self.empty_gitconfig.exists():
            self.empty_gitconfig.write_text("", encoding="utf-8")

    def max_attempts_for(self, step: str) -> int:
        return self.max_attempts.get(step, self.max_attempts["default"])

    def timeout_for(self, step: str) -> int:
        return self.step_timeout_seconds.get(step, self.step_timeout_seconds["default"])


def runtime_info() -> dict:
    """Report honestly which agent runtime is in use. No LLM is called unless explicitly configured."""
    api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    paid_ok = os.environ.get("SF_ALLOW_PAID_INFERENCE") == "1"
    try:
        import anthropic  # noqa: F401
        sdk = True
    except ImportError:
        sdk = False
    return {
        "mode": "simulated",
        "description": "Deterministic simulation: rule-based triage/PRD/plan, search-based program repair "
                       "for coding, and two simulated reviewer adapters. No LLM is called by default.",
        "claude_api_key_present": api_key,
        "anthropic_sdk_installed": sdk,
        "paid_inference_allowed": paid_ok,
        "claude_runtime_available": False,
        "claude_reviewer_available": api_key and paid_ok and sdk,
        "note": "The coding agent has no LLM provider yet. A Claude reviewer adapter exists but is used only "
                "when listed in SF_REVIEWERS and ANTHROPIC_API_KEY, the anthropic SDK and "
                "SF_ALLOW_PAID_INFERENCE=1 are all present.",
    }
