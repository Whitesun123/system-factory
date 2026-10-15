import pytest

from systemfactory.config import Settings
from systemfactory.demo import SAMPLES, init_demo_repo
from systemfactory.orchestrator import Orchestrator, Worker


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    """Tests never depend on the developer's runtime configuration, keys or a running Ollama."""
    for var in ("SF_RUNTIME", "SF_REVIEWERS", "SF_DATA_DIR", "SF_CODER_MODEL", "SF_REVIEWER_A_MODEL",
                "SF_REVIEWER_B_MODEL", "ANTHROPIC_API_KEY", "SF_ALLOW_PAID_INFERENCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SF_OLLAMA_URL", "http://127.0.0.1:9")  # closed port: provider is unreachable


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path / "data")
    init_demo_repo(s)
    return s


@pytest.fixture
def orch(settings):
    return Orchestrator(settings)


def submit_sample(orch, key, **kw):
    s = SAMPLES[key]
    return orch.submit(s["title"], s["body"], source=f"sample:{key}", **kw)


def run_all(orch):
    return Worker(orch, concurrency=1).run_until_idle()
