import time

import pytest

from systemfactory.statemachine import STEP, WF, IllegalTransition, check_transition
from systemfactory.store import Store


def test_legal_and_illegal_transitions():
    check_transition(WF.QUEUED, WF.RUNNING)
    check_transition(WF.RUNNING, WF.AWAITING_APPROVAL)
    check_transition(WF.AWAITING_APPROVAL, WF.APPROVED)
    check_transition(WF.FAILED, WF.QUEUED)
    for bad in [(WF.QUEUED, WF.APPROVED), (WF.RUNNING, WF.APPROVED), (WF.APPROVED, WF.QUEUED),
                (WF.CANCELLED, WF.RUNNING), (WF.REJECTED, WF.APPROVED)]:
        with pytest.raises(IllegalTransition):
            check_transition(*bad)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def test_store_rejects_illegal_transition_and_keeps_state(store):
    wf = store.create_workflow("t", "b", "repo")
    with pytest.raises(IllegalTransition):
        store.transition(wf, WF.APPROVED)
    assert store.get_workflow(wf)["state"] == WF.QUEUED


def test_claim_is_exclusive_and_skips_cancelled(store):
    a = store.create_workflow("a", "", "repo")
    b = store.create_workflow("b", "", "repo")
    store.request_cancel(a)
    assert store.claim_next("w1") == b
    assert store.claim_next("w2") is None
    wf = store.get_workflow(b)
    assert wf["state"] == WF.RUNNING and wf["lease_owner"] == "w1"


def test_recover_stale_requeues_and_resets_running_step(store):
    wf = store.create_workflow("a", "", "repo")
    store.add_steps(wf, [("triage", "triage")])
    store.claim_next("crashed-worker")
    step = store.get_step(wf, "triage")
    store.update_step(step["id"], status=STEP.RUNNING)
    att = store.start_attempt(wf, step["id"], 1)
    store.update_workflow(wf, heartbeat_at=time.time() - 120)
    assert store.recover_stale(stale_seconds=60) == [wf]
    assert store.get_workflow(wf)["state"] == WF.QUEUED
    assert store.get_step(wf, "triage")["status"] == STEP.PENDING
    assert store.get_attempts(wf)[0]["status"] == "interrupted"
    assert any("Recovered stale" in e["message"] for e in store.get_events(wf))
    assert att


def test_events_evidence_usage_are_persisted(store):
    wf = store.create_workflow("a", "", "repo")
    store.log(wf, "hello", step="triage", actor="triage")
    store.add_evidence(wf, "testing", "test_run", "suite", "1 passed", {"exit_code": 0})
    usage = store.add_usage(wf, test_runs=2, candidate_evals=1)
    assert usage["test_runs"] == 2
    assert store.get_evidence(wf)[0]["data"] == {"exit_code": 0}
    assert [e["message"] for e in store.get_events(wf)][-1] == "hello"
