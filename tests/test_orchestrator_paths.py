"""Orchestrator control paths that the scenario tests did not exercise (found by coverage, Milestone 6)."""
import json
import time

import pytest

from systemfactory import cli, orchestrator, roles, statemachine
from systemfactory.context import TransientError
from systemfactory.orchestrator import Orchestrator, Worker
from systemfactory.statemachine import IT, STEP, WF

from .conftest import run_all, submit_sample


def test_submit_rejects_bad_input(orch, tmp_path):
    with pytest.raises(ValueError, match="unknown runtime"):
        orch.submit("t", "b", runtime="gpt")
    with pytest.raises(ValueError, match="not initialised"):
        orch.submit("t", "b", repo=tmp_path / "nowhere")
    with pytest.raises(ValueError, match="title is required"):
        orch.submit("   ", "b")


def test_permanent_error_fails_the_workflow_and_closes_the_iteration(orch):
    wf_id = submit_sample(orch, "pagination", budget={"max_candidate_evals": 1})
    run_all(orch)
    wf = orch.store.workflow(wf_id)
    assert wf["state"] == WF.FAILED and wf["error"].startswith("budget exhausted: candidate")
    assert orch.store.step(wf_id, "coding")["status"] == STEP.FAILED
    assert orch.store.current_iteration(wf_id)["status"] == IT.FAILED
    err = next(e for e in orch.store.get_events(wf_id) if "failed (permanent)" in e["message"])
    assert "Traceback" in err["data"]["traceback"]


def test_orchestrator_bug_never_leaves_a_workflow_running(orch, monkeypatch):
    wf_id = submit_sample(orch, "shipping")
    assert orch.store.claim_next("w") == wf_id

    def broken(_wf_id):
        raise RuntimeError("simulated orchestrator defect")
    monkeypatch.setattr(orch, "_drive", broken)
    assert orch.run_workflow(wf_id) == WF.FAILED
    wf = orch.store.workflow(wf_id)
    assert wf["state"] == WF.FAILED and wf["error"] == "simulated orchestrator defect"
    assert any(e["message"].startswith("Orchestrator error") and e["level"] == "error"
               for e in orch.store.get_events(wf_id))


def test_wall_clock_budget_fails_the_workflow(orch):
    wf_id = submit_sample(orch, "shipping", budget={"max_wall_seconds": 0.01})
    assert orch.store.claim_next("w") == wf_id
    orch.store.update_workflow(wf_id, started_at=time.time() - 5)
    assert orch.run_workflow(wf_id) == WF.FAILED
    assert "wall-clock budget" in orch.store.workflow(wf_id)["error"]


def test_pipeline_without_approval_gate_is_refused(orch, monkeypatch):
    monkeypatch.setitem(statemachine.PIPELINES, "support", [("triage", "triage"), ("support", "support")])
    monkeypatch.setitem(orchestrator.PIPELINES, "support", statemachine.PIPELINES["support"])
    wf_id = submit_sample(orch, "support")
    run_all(orch)
    wf = orch.store.workflow(wf_id)
    assert wf["state"] == WF.FAILED and wf["error"] == "no approval step"


def test_cancellation_during_retry_backoff(orch, monkeypatch):
    original = roles.ROLES["triage"]

    def flaky(ctx):
        orch.store.request_cancel(ctx.wf_id)  # a human cancels while the step is failing
        raise TransientError("flaky tool")
    monkeypatch.setitem(roles.ROLES, "triage", flaky)
    monkeypatch.setitem(orchestrator.ROLES, "triage", flaky)
    wf_id = submit_sample(orch, "shipping")
    run_all(orch)
    assert orch.store.workflow(wf_id)["state"] == WF.CANCELLED
    assert orch.store.step(wf_id, "triage")["status"] == STEP.CANCELLED
    assert [a["status"] for a in orch.store.get_attempts(wf_id)] == ["failed"]  # no second attempt
    assert original is not flaky


def test_retry_of_a_blocked_non_loop_step(orch):
    wf_id = submit_sample(orch, "not_reproducible")
    run_all(orch)
    assert orch.store.step(wf_id, "reproduce")["status"] == STEP.BLOCKED
    orch.retry(wf_id)
    assert orch.store.step(wf_id, "reproduce")["status"] == STEP.PENDING
    run_all(orch)
    assert orch.store.workflow(wf_id)["state"] == WF.BLOCKED  # still not reproducible: honest repeat


def test_retry_after_iteration_limit_grants_one_more_iteration(orch):
    wf_id = submit_sample(orch, "bulk", budget={"max_iterations": 1})
    run_all(orch)
    assert orch.store.workflow(wf_id)["state"] == WF.BLOCKED
    orch.retry(wf_id)
    assert orch.store.workflow(wf_id)["budget"]["max_iterations"] == 2
    run_all(orch)
    wf = orch.store.workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL and wf["data"]["quality_gate"]["status"] == "SUCCESS"


def test_closing_an_iteration_falls_back_to_failed_on_illegal_transition(orch):
    wf_id = submit_sample(orch, "shipping")
    it = orch.store.open_iteration(wf_id)
    orch.store.update_iteration(wf_id, it["n"], status=IT.TESTING)
    orch._close_iteration(wf_id, IT.STALLED, "testing -> stalled is not a legal transition")
    assert orch.store.current_iteration(wf_id)["status"] == IT.FAILED


def test_background_worker_recovers_a_crashed_run_and_processes_the_queue(settings):
    orch = Orchestrator(settings)
    crashed = submit_sample(orch, "support")
    assert orch.store.claim_next("worker-that-crashed") == crashed
    orch.store.update_workflow(crashed, heartbeat_at=time.time() - 120)
    queued = submit_sample(orch, "shipping")
    worker = Worker(orch, concurrency=2)
    worker.start()
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            states = {orch.store.workflow(i)["state"] for i in (crashed, queued)}
            if states <= {WF.AWAITING_APPROVAL}:
                break
            time.sleep(0.5)
    finally:
        worker.stop()
    assert orch.store.workflow(crashed)["state"] == WF.AWAITING_APPROVAL
    assert orch.store.workflow(queued)["state"] == WF.AWAITING_APPROVAL
    assert any("Recovered stale workflow" in e["message"] for e in orch.store.get_events(crashed))


def test_provenance_tolerates_missing_git(monkeypatch):
    def no_git(*a, **k):
        raise OSError("git not installed")
    monkeypatch.setattr(orchestrator.subprocess, "run", no_git)
    prov = orchestrator.source_provenance()
    assert prov["source_revision"] is None and prov["source_dirty"] is None


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SF_DATA_DIR", str(tmp_path / "cli-data"))
    cli.main(["init-demo"])
    cli.main(["submit", "--sample", "shipping"])
    cli.main(["run"])
    cli.main(["status"])
    cli.main(["show", "1"])
    cli.main(["approve", "1", "--by", "Demo Approver"])
    cli.main(["export", "1", "--out", str(tmp_path / "ev")])
    cli.main(["runtime"])
    out = capsys.readouterr().out
    assert "submitted workflow 1" in out and "workflow 1: approved" in out and "evidence written to" in out
    manifest = json.loads((tmp_path / "ev" / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["workflow"]["state"] == "approved"
    assert '"mode": "simulated"' in out
