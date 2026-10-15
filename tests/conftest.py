import pytest

from systemfactory.config import Settings
from systemfactory.demo import SAMPLES, init_demo_repo
from systemfactory.orchestrator import Orchestrator, Worker


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
