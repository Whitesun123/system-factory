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
        "max_llm_tokens": 600_000,     # local tokens are free but still bounded
        "max_cost_usd": 1.00,
        "stall_window": 2,          # consecutive iterations without fewer blocking findings
    })
    max_attempts: dict = field(default_factory=lambda: {"coding": 3, "review": 2, "default": 2})
    step_timeout_seconds: dict = field(default_factory=lambda: {"coding": 240, "review": 300, "default": 120})
    # Reviewer adapters, in order. Overridable per workflow (options.reviewers) or via SF_REVIEWERS.
    reviewers: list = field(default_factory=lambda: [
        r.strip() for r in os.environ.get("SF_REVIEWERS", "simulated-spec,simulated-diff").split(",") if r.strip()])
    min_reviewers: int = 2
    # Runtime: "simulated" (deterministic; default, used by the unit tests) or "ollama" (real local inference).
    runtime: str = field(default_factory=lambda: os.environ.get("SF_RUNTIME", "simulated"))
    ollama_url: str = field(default_factory=lambda: os.environ.get("SF_OLLAMA_URL", "http://127.0.0.1:11434"))
    coder_model: str = field(default_factory=lambda: os.environ.get("SF_CODER_MODEL", "qwen2.5-coder:14b"))
    reviewer_a_model: str = field(default_factory=lambda: os.environ.get("SF_REVIEWER_A_MODEL", "phi4:14b"))
    reviewer_b_model: str = field(default_factory=lambda: os.environ.get("SF_REVIEWER_B_MODEL", "gemma3:12b"))
    coder_max_turns: int = field(default_factory=lambda: int(os.environ.get("SF_CODER_MAX_TURNS", "6")))
    # Execution backend for target/model-generated code: "local" (process isolation) or "docker" (container).
    execution: str = field(default_factory=lambda: os.environ.get("SF_EXECUTION", "local"))

    def runtime_options(self, runtime: str) -> dict:
        """Workflow options that select a runtime; stored on the workflow so the run records its configuration."""
        if runtime == "ollama":
            # Model-written patches and checks are untrusted code: container isolation is required by default.
            return {"runtime": "ollama", "coder": "ollama", "reviewers": ["ollama-a", "ollama-b"],
                    "allow_simulated_reviewers": False, "ollama_url": self.ollama_url,
                    "execution": "docker", "require_isolation": True,
                    "models": {"coder": self.coder_model, "reviewer_a": self.reviewer_a_model,
                               "reviewer_b": self.reviewer_b_model}}
        return {"runtime": "simulated"}
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


def runtime_info(settings: "Settings | None" = None, probe_ollama: bool = True) -> dict:
    """Report honestly which runtime is configured and which providers are actually reachable."""
    settings = settings or Settings.from_env()
    api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    paid_ok = os.environ.get("SF_ALLOW_PAID_INFERENCE") == "1"
    try:
        import anthropic  # noqa: F401
        sdk = True
    except ImportError:
        sdk = False
    models = {"coder": settings.coder_model, "reviewer_a": settings.reviewer_a_model,
              "reviewer_b": settings.reviewer_b_model}
    ollama = None
    if probe_ollama:
        from .llm import OllamaClient
        ollama = OllamaClient(base_url=settings.ollama_url).status(sorted(set(models.values())))
    descriptions = {
        "simulated": "Deterministic simulation: rule-based triage/PRD/plan, search-based program repair for coding "
                     "and two simulated reviewers. No LLM is called.",
        "ollama": "Real local inference via Ollama: LLM coding agent and two LLM reviewers (one provider, "
                  "different models). Triage/PRD/plan, tests and the quality gate stay deterministic.",
    }
    return {
        "mode": settings.runtime, "available_modes": ["simulated", "ollama"],
        "description": descriptions.get(settings.runtime, settings.runtime),
        "models": models, "ollama": ollama,
        "claude_api_key_present": api_key, "anthropic_sdk_installed": sdk, "paid_inference_allowed": paid_ok,
        "claude_reviewer_available": api_key and paid_ok and sdk,
        "note": "Paid cloud providers (Claude adapter, command adapter) stay inactive unless configured and "
                "explicitly approved (SF_ALLOW_PAID_INFERENCE=1).",
    }
