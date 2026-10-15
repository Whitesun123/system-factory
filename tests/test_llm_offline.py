"""Milestone 5 provider / evidence tests that need NO running LLM.

The "provider unavailable" cases point at a closed local port, so the real OllamaClient error path runs
without any inference. Tests that need a live Ollama server are not part of this suite (see
scripts/real_demo.py and MILESTONE5_STATUS.md).
"""
import json

import pytest

from systemfactory import loop, reviewers
from systemfactory.evidence import Sanitizer, export
from systemfactory.llm import OllamaClient, ProviderUnavailable, Recorder, parse_json, redact
from systemfactory.statemachine import WF

from .conftest import run_all, submit_sample

DEAD = "http://127.0.0.1:9"  # nothing listens here


def test_unreachable_ollama_is_reported_not_faked(orch):
    c = OllamaClient(base_url=DEAD, timeout=2)
    st = c.status(["qwen2.5-coder:14b"])
    assert st["reachable"] is False and "not reachable" in st["error"]
    with pytest.raises(ProviderUnavailable):
        c.chat("qwen2.5-coder:14b", [{"role": "user", "content": "hi"}])
    ok, why = reviewers.OllamaReviewer("ollama-a", "phi4:14b", base_url=DEAD).available()
    assert not ok and "not reachable" in why


def test_recorder_persists_failed_calls(orch):
    wf_id = submit_sample(orch, "shipping")
    rec = Recorder(orch.store, wf_id, 1, "coding", "coding")
    with pytest.raises(ProviderUnavailable):
        rec.chat(OllamaClient(base_url=DEAD, timeout=2), "qwen2.5-coder:14b", [{"role": "user", "content": "x"}])
    (call,) = orch.store.get_llm_calls(wf_id)
    assert call["status"] == "unavailable" and call["execution"] == "real-local" and call["input_tokens"] is None


def test_real_runtime_with_unavailable_provider_fails_honestly(orch):
    """Scenario D offline: runtime=ollama but the server is down -> retries, then FAILED with the real error."""
    wf_id = submit_sample(orch, "bulk", runtime="ollama",  # trusted demo repo, no model output executes
                          options={"ollama_url": DEAD, "execution": "local", "require_isolation": False})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.FAILED and "not reachable" in wf["error"]
    calls = orch.store.get_llm_calls(wf_id)
    assert len(calls) == 3 and all(c["status"] == "unavailable" for c in calls)  # one per coding attempt
    assert not orch.store.get_reviews(wf_id)  # nothing was reviewed, nothing was simulated in its place
    assert wf["options"]["models"]["coder"] == "qwen2.5-coder:14b"


def test_real_runtime_does_not_substitute_simulated_reviewers():
    chosen, notes = reviewers.resolve(["ollama-a", "ollama-b"], 2, {"ollama_url": DEAD}, allow_simulated=False)
    assert chosen == [] and len(notes) == 2


def test_check_to_probe_accepts_only_safe_calls():
    inv = reviewers.function_inventory({"shopcart/checkout.py": "def shipping_banner(x):\n    return x\n"})
    good = reviewers.check_to_probe({"function": "shipping_banner", "call": "shipping_banner(50.0)",
                                     "expected": "Your order ships free!"}, inv)
    assert good == ">>> from shopcart.checkout import shipping_banner\n>>> shipping_banner(50.0)\n'Your order ships free!'\n"
    assert reviewers.check_to_probe({"function": "shipping_banner", "call": "shipping_banner(list(range(3)))",
                                     "expected": "[0, 1, 2]"}, inv)
    for bad in ["__import__('os').system('x')", "shipping_banner(open('f'))", "other(1)", "shipping_banner(x)"]:
        assert reviewers.check_to_probe({"function": "shipping_banner", "call": bad, "expected": "1"}, inv) is None


def test_probe_classification():
    assert loop.classify_probe(True, "ok") == "pass"
    assert loop.classify_probe(False, "Expected:\n    1\nGot:\n    2\n") == "mismatch"
    assert loop.classify_probe(False, "Exception raised:\n    Traceback...\n    NameError: name 'f'") == "probe-error"
    assert loop.classify_probe(False, "Exception raised:\n    Traceback...\n    ZeroDivisionError: x") == "exception"


def test_confirmed_check_upgrades_and_unverified_llm_blocker_downgrades():
    tests = {"repro_passed": True, "suite_passed": True, "probes": [], "security": {"findings": []}}
    review = {"reviewer": "ollama-a", "verdict": "approve", "simulated": False,
              "findings": [{"severity": "blocking", "title": "claim without evidence", "file": "f.py", "function": "g"}],
              "checks": [{"function": "h", "call": "h(1)", "expected": "2", "file": "f.py",
                          "probe": ">>> from f import h\n>>> h(1)\n2\n"}]}
    agg = loop.aggregate([review], tests, lambda src: (False, "Expected:\n    2\nGot:\n    1\n"))
    sev = {i["location"]["where"]: i["severity"] for i in agg["items"]}
    assert sev == {"h": "blocking", "g": "major"}
    assert agg["checks"][0]["outcome"] == "mismatch" and agg["checks"][0]["got"] == "1"


def test_redaction_and_json_parsing():
    assert "sk-ant" not in redact("key sk-ant-abcdefghijklmnop123")
    assert parse_json('noise {"action": "run_tests"} trailing') == {"action": "run_tests"}


def test_evidence_export_is_complete_and_sanitized(orch, tmp_path):
    wf_id = submit_sample(orch, "bulk")
    run_all(orch)
    out = export(orch, wf_id, tmp_path / "ev")
    names = {p.name for p in out.iterdir()}
    assert {"summary.md", "run-manifest.json", "iterations.json", "review-findings.json", "test-results.json",
            "quality-gate.json", "patch.diff", "llm-calls.json", "tool-calls.json", "events.json"} <= names
    blob = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    assert str(orch.settings.data_dir) not in blob and str(orch.settings.data_dir).replace("\\", "/") not in blob
    manifest = json.loads((out / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution_modes"] == ["simulated"] and manifest["totals"]["llm_calls"] == 0
    tests = json.loads((out / "test-results.json").read_text(encoding="utf-8"))
    assert tests and all("exit_code" in t and "commit_sha" in t for t in tests)
    assert Sanitizer(orch.settings.data_dir).text("mail me@corp.example") == "mail <email>"
