"""Regressions for defects found in the final release audit (October 2026)."""
import pytest

from systemfactory import coders
from systemfactory.llm import InvalidModelOutput, parse_json
from systemfactory.statemachine import WF

from .conftest import run_all, submit_sample
from .test_security_regressions import ScriptedModel


@pytest.mark.parametrize("reply", ["[1, 2]", '"submit"', "42", "null"])
def test_json_that_is_not_an_object_is_invalid_model_output(reply):
    with pytest.raises(InvalidModelOutput, match="not an object"):
        parse_json(reply)


def test_malformed_coder_actions_are_feedback_not_a_crash(orch, monkeypatch):
    # Before the fix, a reply that was valid JSON of the wrong shape raised AttributeError/TypeError inside the
    # coding step and failed the whole workflow permanently.
    model = ScriptedModel([
        [1, 2],                                                         # JSON, but not an object
        {"action": "submit", "edits": "change the threshold"},          # edits is not a list
        {"action": "submit", "edits": [{"file": "shopcart/cart.py", "search": 5, "replace": None}]},
        {"action": "read_file", "path": 123},                           # path is not a string
        {"action": "submit", "summary": "real fix", "edits": [
            {"file": "shopcart/cart.py", "search": "if self.subtotal() > FREE_SHIPPING_THRESHOLD:",
             "replace": "if self.subtotal() >= FREE_SHIPPING_THRESHOLD:"}]},
    ])
    monkeypatch.setattr(coders, "OllamaClient", lambda base_url=None, timeout=None: model)
    wf_id = submit_sample(orch, "shipping", runtime="ollama",
                          options={"reviewers": ["simulated-spec", "simulated-diff"], "allow_simulated_reviewers": True,
                                   "execution": "local", "require_isolation": False})  # scripted test double
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL and wf["data"]["quality_gate"]["status"] == "SUCCESS"
    tools = [(t["tool"], t["ok"]) for t in orch.store.get_tool_calls(wf_id)]
    assert tools == [("invalid", False), ("submit", False), ("submit", False), ("read_file", False), ("submit", True)]


def test_dashboard_runtime_selector_starts_on_the_server_default():
    # Found in the fresh-clone smoke test: the selector defaulted to "Real local inference (Ollama)" even with no
    # Ollama, so "pick a sample and press Submit" started a real-runtime workflow that could only fail.
    import re
    from pathlib import Path

    html = (Path(__file__).resolve().parent.parent / "systemfactory" / "static" / "index.html").read_text(encoding="utf-8")
    select = re.search(r'<select id="runtimeSel">(.*?)</select>', html, re.S).group(1)
    assert re.findall(r'<option value="(\w+)"', select)[0] == "simulated"
    assert '$("#runtimeSel").value = rt.mode' in html  # then follows SF_RUNTIME from /api/health, once


def test_relative_data_dir_is_resolved_and_workflows_still_run(tmp_path, monkeypatch):
    # Found in the fresh-clone smoke test: SF_DATA_DIR=../cli-data made every bug workflow fail at `workspace`.
    from systemfactory.config import Settings
    from systemfactory.demo import init_demo_repo
    from systemfactory.orchestrator import Orchestrator

    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setenv("SF_DATA_DIR", "../rel-data")
    s = Settings.from_env()
    assert s.data_dir == (tmp_path / "rel-data").resolve() and s.data_dir.is_absolute()
    init_demo_repo(s)
    orch = Orchestrator(s)
    wf_id = submit_sample(orch, "shipping")
    run_all(orch)
    assert orch.store.get_workflow(wf_id)["state"] == WF.AWAITING_APPROVAL
