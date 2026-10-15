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
        "max_wall_seconds": 600,
        "max_candidate_evals": 400,
        "max_test_runs": 600,
    })
    max_attempts: dict = field(default_factory=lambda: {"coding": 3, "default": 2})
    step_timeout_seconds: dict = field(default_factory=lambda: {"coding": 240, "default": 120})
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
    return {
        "mode": "simulated",
        "description": "Deterministic simulation: rule-based triage/PRD/plan/review and "
                       "search-based program repair for coding. No LLM is called.",
        "claude_api_key_present": api_key,
        "paid_inference_allowed": paid_ok,
        "claude_runtime_available": False,
        "note": "A Claude API provider is not implemented yet; it requires an API key and "
                "explicit budget approval (SF_ALLOW_PAID_INFERENCE=1).",
    }
