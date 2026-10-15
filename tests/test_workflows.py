"""End-to-end workflows against the real demo repository (git worktrees, real pytest runs)."""
import threading
import time

import pytest

from systemfactory import roles
from systemfactory.context import PermanentError, StepContext
from systemfactory.statemachine import STEP, WF, IllegalTransition

from .conftest import run_all, submit_sample


def steps_by_name(orch, wf_id):
    return {s["name"]: s for s in orch.store.get_steps(wf_id)}


def main_head(orch):
    return orch.git.out(orch.settings.demo_repo, "rev-parse", "main")


def test_bug_to_verified_fix_then_human_approval(orch):
    head_before = main_head(orch)
    wf_id = submit_sample(orch, "shipping")
    run_all(orch)

    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL and wf["kind"] == "bug"
    data = wf["data"]
    assert data["triage"]["components"] == ["shopcart/cart.py"]
    assert "failed" in data["reproduce"]["base_failure"]
    assert data["coding"]["mutation"].endswith("`>` -> `>=`") and data["coding"]["tier"] == 1
    assert data["testing"]["repro_passed"] and data["testing"]["suite_passed"]
    assert data["testing"]["security"]["files_changed"] == ["shopcart/cart.py"]
    assert data["feedback"]["decision"] == "pass" and data["feedback"]["iteration"] == 1
    assert data["quality_gate"]["status"] == "SUCCESS"

    kinds = [e["kind"] for e in orch.store.get_evidence(wf_id)]
    for k in ("prd", "plan", "test_run", "repair_search", "patch", "review", "feedback", "diff", "quality_gate"):
        assert k in kinds
    # fix lives on the workflow branch in its own worktree; main is untouched
    branch_file = orch.git.out(wf["worktree"], "show", "HEAD:shopcart/cart.py")
    assert "self.subtotal() >= FREE_SHIPPING_THRESHOLD" in branch_file
    assert main_head(orch) == head_before

    with pytest.raises(ValueError):
        orch.approve(wf_id, "  ")
    orch.approve(wf_id, "Demo Approver", "looks right")
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.APPROVED
    assert orch.store.get_approvals(wf_id)[0]["actor"] == "Demo Approver"
    assert main_head(orch) == head_before  # approval never merges
    with pytest.raises(IllegalTransition):
        orch.reject(wf_id, "Demo Approver")


def test_failed_attempt_is_retried_with_wider_search(orch):
    wf_id = submit_sample(orch, "pagination")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL
    coding = [a for a in orch.store.get_attempts(wf_id) if a["step"] == "coding"]
    assert [a["status"] for a in coding] == ["failed", "succeeded"]
    assert "Tier 1" in coding[0]["error"]
    assert wf["data"]["coding"]["tier"] == 2
    assert "drop term `- 1`" in wf["data"]["coding"]["mutation"]


def test_injected_transient_fault_is_retried(orch):
    wf_id = submit_sample(orch, "shipping", options={"faults": {"reproduce": 1}})
    run_all(orch)
    assert orch.store.get_workflow(wf_id)["state"] == WF.AWAITING_APPROVAL
    rep = [a for a in orch.store.get_attempts(wf_id) if a["step"] == "reproduce"]
    assert [a["status"] for a in rep] == ["failed", "succeeded"]
    assert "INJECTED" in rep[0]["error"]


def test_unfixable_bug_exhausts_retries_and_fails_with_evidence(orch):
    wf_id = submit_sample(orch, "coupon")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.FAILED and "retries exhausted after 3 attempts" in wf["error"]
    assert steps_by_name(orch, wf_id)["coding"]["attempts"] == 3
    searches = [e for e in orch.store.get_evidence(wf_id) if e["kind"] == "repair_search"]
    assert len(searches) == 3
    assert orch.git.out(wf["worktree"], "status", "--porcelain") == ""  # all candidates reverted

    orch.retry(wf_id)  # human retry is allowed and re-queues from the failed step
    assert orch.store.get_workflow(wf_id)["state"] == WF.QUEUED
    assert steps_by_name(orch, wf_id)["coding"]["status"] == STEP.PENDING


def test_unreproducible_bug_blocks(orch):
    wf_id = submit_sample(orch, "not_reproducible")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.BLOCKED and "Could not reproduce" in wf["error"]
    assert "coding" not in {s["name"] for s in orch.store.get_steps(wf_id) if s["status"] != STEP.PENDING}


def test_feature_request_blocks_honestly_at_coding(orch):
    wf_id = submit_sample(orch, "feature")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["kind"] == "feature" and wf["state"] == WF.BLOCKED
    assert steps_by_name(orch, wf_id)["coding"]["status"] == STEP.BLOCKED
    assert "LLM coding runtime" in wf["error"]


def test_support_question_gets_draft_answer_for_approval(orch):
    wf_id = submit_sample(orch, "support")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["kind"] == "support" and wf["state"] == WF.AWAITING_APPROVAL
    assert wf["data"]["support"]["source"] == "README.md#Applying a coupon code"
    assert "coupon_discount" in wf["data"]["support"]["answer"]
    assert wf["worktree"] is None  # no code workspace for support work


def test_cancel_queued_and_running_workflows(orch):
    queued = submit_sample(orch, "shipping")
    orch.cancel(queued)
    assert orch.store.get_workflow(queued)["state"] == WF.CANCELLED

    wf_id = submit_sample(orch, "coupon", options={"throttle_ms": 150})
    worker = threading.Thread(target=run_all, args=(orch,))
    worker.start()
    deadline = time.time() + 60
    while time.time() < deadline:
        coding = orch.store.get_step(wf_id, "coding")
        if coding and coding["status"] == STEP.RUNNING and orch.store.get_workflow(wf_id)["usage"]["candidate_evals"] >= 1:
            break
        time.sleep(0.05)
    else:
        pytest.fail("coding step never started")
    orch.cancel(wf_id)
    worker.join(timeout=30)
    assert not worker.is_alive()
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.CANCELLED
    assert orch.store.get_step(wf_id, "coding")["status"] == STEP.CANCELLED
    assert [a["status"] for a in orch.store.get_attempts(wf_id) if a["step"] == "coding"][-1] == "cancelled"
    assert orch.git.out(wf["worktree"], "status", "--porcelain") == ""  # mutated file restored
    assert orch.store.current_iteration(wf_id)["status"] == "cancelled"
    with pytest.raises(IllegalTransition):
        orch.cancel(wf_id)


def test_quality_gate_rejects_test_tampering_and_dangerous_code(orch):
    wf_id = submit_sample(orch, "shipping")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    wt = wf["worktree"]
    # Simulate a misbehaving coding agent: edit the repro test and add eval() to source.
    (orch.settings.worktrees_dir / f"wf-{wf_id}" / "tests" / "test_cart.py").write_text("def test_x():\n    pass\n")
    with open(f"{wt}/shopcart/cart.py", "a", encoding="utf-8") as f:
        f.write("\nRESULT = eval('1+1')\n")
    orch.git.run(wt, "commit", "-qam", "tamper")
    ctx = StepContext(orch.settings, orch.store, orch.git, wf_id, "quality_gate", "quality-gate", 1, time.time() + 60)
    with pytest.raises(PermanentError, match="quality gate failed: scope, security"):
        roles.quality_gate(ctx)
    report = orch.store.get_evidence(wf_id)[-1]
    assert report["kind"] == "quality_gate" and report["data"]["status"] == "FAILED"
    assert "coding agent changed tests/test_cart.py" in report["content"] and "eval(" in report["content"]
    sec = roles.scan_diff(ctx, f"{wf['data']['reproduce']['commit']}..HEAD")
    assert {f["rule"] for f in sec["findings"]} >= {"tests-modified", "dangerous-call"}


def test_concurrent_workers_do_not_double_process(orch):
    from systemfactory.orchestrator import Worker
    ids = [submit_sample(orch, "shipping"), submit_sample(orch, "support"), submit_sample(orch, "feature")]
    workers = [Worker(orch, concurrency=1) for _ in range(3)]
    threads = [threading.Thread(target=w.run_until_idle) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    states = {i: orch.store.get_workflow(i)["state"] for i in ids}
    assert states == {ids[0]: WF.AWAITING_APPROVAL, ids[1]: WF.AWAITING_APPROVAL, ids[2]: WF.BLOCKED}
    for i in ids:
        claims = [e for e in orch.store.get_events(i) if "claimed by" in e["message"]]
        assert len(claims) == 1
