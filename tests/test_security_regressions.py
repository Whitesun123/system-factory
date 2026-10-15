"""Regression tests for issues found in the Milestone 5.1 security review (F1-F6)."""
import json
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from systemfactory import coders, reviewers, sandbox
from systemfactory.api import create_app
from systemfactory.llm import ChatResult
from systemfactory.statemachine import WF, IllegalTransition

from .conftest import run_all, submit_sample

INV = reviewers.function_inventory({"shopcart/checkout.py": "def shipping_banner(x):\n    return x\n"})


def test_f1_multiline_expected_cannot_inject_doctest_lines():
    evil = "'''x\n>>> __import__('os').system('echo pwned')\n'''"
    probe = reviewers.check_to_probe({"function": "shipping_banner", "call": "shipping_banner(1)", "expected": evil}, INV)
    lines = probe.splitlines()
    assert len(lines) == 3 and sum(ln.startswith(">>> ") for ln in lines) == 2  # import + call only
    assert not lines[2].startswith(">>>") and "__import__" in lines[2]  # the payload is inert text in a repr


@pytest.mark.parametrize("probe", [
    ">>> import os\n>>> os.getcwd()\n'x'\n",
    ">>> __import__('os').system('x')\n0\n",
    ">>> open('secret.txt').read()\n''\n",
    ">>> from os import system\n>>> system('x')\n0\n",
    ">>> from shopcart.checkout import shipping_banner\n>>> shipping_banner.__globals__\n{}\n",
    ">>> eval('1+1')\n2\n",
    "no examples at all",
])
def test_f2_unsafe_model_probes_are_rejected(probe):
    assert not reviewers.probe_is_safe(probe, {"shopcart.checkout"})


def test_f2_ordinary_model_probe_is_accepted():
    ok = ">>> from shopcart.checkout import shipping_banner\n>>> b = shipping_banner(50.0)\n>>> b.upper()\n'X'\n"
    assert reviewers.probe_is_safe(ok, {"shopcart.checkout"})


def test_f2_untrusted_reviewer_probe_is_never_executed(orch, monkeypatch, tmp_path):
    marker = tmp_path / "pwned.txt"

    class Malicious:
        name, provider, model, simulated, technique = "malicious", "test", "double", True, "returns an unsafe probe"
        def available(self): return True, ""
        def review(self, bundle, tools):
            return {"verdict": "request_changes", "findings": [{
                "severity": "blocking", "title": "trust me", "file": "shopcart/cart.py", "function": "shipping",
                "probe": f">>> open(r'{marker}', 'w').write('x')\n1\n"}]}

    monkeypatch.setitem(reviewers.FACTORIES, "malicious", Malicious)
    wf_id = submit_sample(orch, "shipping", options={"reviewers": ["simulated-spec", "malicious"]})
    run_all(orch)
    assert not marker.exists()
    f = next(f for r in orch.store.get_reviews(wf_id) if r["reviewer"] == "malicious" for f in r["findings"])
    assert f["probe"] is None and "rejected by safety vetting" in f["detail"]


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, start_worker=False), base_url="http://127.0.0.1:8765") as c:
        yield c


def test_f3_foreign_host_header_is_refused(client):
    assert client.get("/api/health", headers={"host": "attacker.example"}).status_code == 403  # DNS rebinding
    assert client.get("/api/health", headers={"host": "localhost:8765"}).status_code == 200


def test_f3_cross_origin_state_change_is_refused(client):
    wf = client.post("/api/samples/shipping", json={}).json()["workflow_id"]
    r = client.post(f"/api/workflows/{wf}/cancel", headers={"origin": "https://attacker.example"})
    assert r.status_code == 403
    assert client.post(f"/api/workflows/{wf}/cancel", headers={"origin": "null"}).status_code == 403
    assert client.post(f"/api/workflows/{wf}/cancel", headers={"origin": "http://127.0.0.1:8765"}).json() == {"state": "cancelled"}


def test_f4_concurrent_approvals_record_exactly_one_decision(orch):
    wf_id = submit_sample(orch, "shipping")
    run_all(orch)
    barrier, errors = threading.Barrier(2), []

    def decide(fn, who):
        barrier.wait()
        try:
            fn(wf_id, who)
        except IllegalTransition as e:
            errors.append(e)

    ts = [threading.Thread(target=decide, args=(orch.approve, "A")), threading.Thread(target=decide, args=(orch.reject, "B"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(errors) == 1 and len(orch.store.get_approvals(wf_id)) == 1
    final = orch.store.get_workflow(wf_id)["state"]
    assert final in (WF.APPROVED, WF.REJECTED)
    assert orch.store.get_approvals(wf_id)[0]["decision"] == ("approved" if final == WF.APPROVED else "rejected")


class ScriptedModel:
    """Test double standing in for the Ollama client: replays fixed actions. Recorded as 'simulated'."""
    provider, execution, base_url, timeout = "test-double", "simulated", "scripted", 1

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def model_info(self, model):
        return {"digest": None, "details": {}}

    def chat(self, model, messages, schema=None, options=None, timeout=None):
        self.seen.append(messages[-1]["content"])
        return ChatResult(json.dumps(self.replies.pop(0)), model, 0, 0, 0.0, 0.0, 0.0)


def test_case_c_llm_coder_cannot_edit_tests_or_escape_scope(orch, monkeypatch):
    model = ScriptedModel([
        {"action": "read_file", "path": "../../outside.txt"},
        {"action": "submit", "summary": "weaken the test", "edits": [
            {"file": "tests/test_cart.py", "search": "assert cart.shipping() == 0.0", "replace": "pass"}]},
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
    tools = orch.store.get_tool_calls(wf_id)
    assert [(t["tool"], t["ok"]) for t in tools] == [("read_file", False), ("submit", False), ("submit", True)]
    assert "outside the allowed files" in tools[1]["result"]
    assert wf["data"]["testing"]["security"]["files_changed"] == ["shopcart/cart.py"]
    assert all(c["execution"] == "simulated" for c in orch.store.get_llm_calls(wf_id))  # honest label for the double


@pytest.mark.skipif(sys.platform == "win32", reason="process-group kill is the POSIX code path")
def test_f6_timeout_kills_grandchildren():
    code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)"
    res = sandbox.run_command([sys.executable, "-c", code], ".", timeout=1, env=sandbox.scrubbed_env())
    assert res.timed_out and res.duration < 10  # communicate() returned: no orphan kept the pipe open
