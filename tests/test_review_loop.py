"""Iterative coding -> tests -> independent reviewers -> feedback -> revision loop.

All reviewers here are deterministic simulations or labelled test doubles; none is a real model.
"""
import json
import sys
from types import SimpleNamespace

import pytest

from systemfactory import loop, reviewers
from systemfactory.statemachine import IT, STEP, WF, IllegalTransition, check_iteration_transition

from .conftest import run_all, submit_sample


# ------------------------------------------------------------------ the headline demonstration
def test_first_implementation_fails_review_then_is_corrected(orch):
    wf_id = submit_sample(orch, "bulk")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL

    it1, it2 = orch.store.get_iterations(wf_id)
    # iteration 1: tests pass, but the independent spec reviewer blocks it with specific, evidence-backed feedback
    assert it1["status"] == IT.REVISE and it1["tests"]["all_passed"] and it1["blocking_count"] == 1
    item = it1["feedback"]["items"][0]
    assert item["severity"] == "blocking" and item["location"]["where"] == "bulk_discount_percent"
    assert item["corroborated"] and item["sources"] == ["simulated-diff", "simulated-spec"]
    spec = next(f for f in item["findings"] if f["reviewer"] == "simulated-spec")
    assert spec["confirmed"] is True and "`bulk_discount_percent(50)` is documented to return `10.0`" in spec["detail"]
    assert "bulk_discount_percent(50)" in it1["feedback"]["probes"][0]
    assert it1["feedback"]["strategy"] == "build_on_previous"

    # iteration 2: the coder received the feedback, built on its previous patch and satisfied the probe
    assert it2["status"] == IT.PASSED and it2["blocking_count"] == 0
    assert it2["patch"]["strategy"] == "build_on_previous" and it2["patch"]["addressed"] == ["F1"]
    assert it2["patch"]["required_probes"] == 1 and it2["base"] == it1["commit"]
    assert it2["fingerprint"] != it1["fingerprint"]
    assert it2["tests"]["probes"][0]["passed"]
    src = orch.git.out(wf["worktree"], "show", "HEAD:shopcart/cart.py")
    assert "quantity >= 50" in src and "quantity >= 10" in src

    # two independent reviewers per iteration, honestly labelled simulated
    for n in (1, 2):
        revs = orch.store.get_reviews(wf_id, n)
        assert [r["reviewer"] for r in revs] == ["simulated-spec", "simulated-diff"]
        assert all(r["simulated"] for r in revs)
    assert [r["verdict"] for r in orch.store.get_reviews(wf_id, 1)] == ["request_changes", "approve"]
    div = wf["data"]["review"]["diversity"]
    assert div["real_provider_diversity"] is False and div["label"].startswith("SIMULATED")

    # attempt history is grouped by iteration
    coding = [(a["iteration"], a["status"]) for a in orch.store.get_attempts(wf_id) if a["step"] == "coding"]
    assert coding == [(1, "succeeded"), (2, "succeeded")]

    gate = wf["data"]["quality_gate"]
    assert gate["status"] == "SUCCESS" and gate["iterations"] == 2
    assert all(c["ok"] for c in gate["criteria"]) and all(c["ok"] for c in gate["checks"].values())
    orch.approve(wf_id, "Demo Approver")
    assert orch.store.get_workflow(wf_id)["state"] == WF.APPROVED


def test_repeated_solution_is_detected_and_escalated(orch):
    wf_id = submit_sample(orch, "bulk", options={"coder": "simulated-stubborn"})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.BLOCKED and "Repeated solution" in wf["error"]
    it1, it2 = orch.store.get_iterations(wf_id)
    assert it1["fingerprint"] == it2["fingerprint"] and it2["status"] == IT.STALLED
    assert any(f["rule"] == "repeated-solution" for i in it2["feedback"]["items"] for f in i["findings"])
    esc = wf["data"]["escalation"]
    assert esc["decision"] == "stalled" and esc["open_blocking"]
    assert orch.store.get_step(wf_id, "feedback")["status"] == STEP.BLOCKED

    # a human retries with a cooperative coder: the loop resumes at coding and converges
    orch.store.update_workflow(wf_id, options={**wf["options"], "coder": "simulated-repair"})
    orch.retry(wf_id)
    assert orch.store.get_step(wf_id, "coding")["status"] == STEP.PENDING
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL
    assert [i["status"] for i in orch.store.get_iterations(wf_id)] == [IT.REVISE, IT.STALLED, IT.PASSED]


def test_iteration_limit_escalates_instead_of_looping(orch):
    wf_id = submit_sample(orch, "bulk", budget={"max_iterations": 1})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.BLOCKED and "Iteration limit reached (1/1)" in wf["error"]
    (it1,) = orch.store.get_iterations(wf_id)
    assert it1["status"] == IT.EXHAUSTED
    with pytest.raises(IllegalTransition):
        orch.approve(wf_id, "Demo Approver")


def test_approval_requires_quality_gate_success(orch):
    wf_id = submit_sample(orch, "shipping")
    run_all(orch)
    orch.store.merge_data(wf_id, "quality_gate", {"status": "FAILED"})
    with pytest.raises(IllegalTransition, match="quality gate"):
        orch.approve(wf_id, "Demo Approver")
    orch.reject(wf_id, "Demo Approver", "rejecting is always allowed")
    assert orch.store.get_workflow(wf_id)["state"] == WF.REJECTED


# ------------------------------------------------------------------ adapters (test doubles, labelled simulated)
class FakeClaudeClient:
    """Stands in for anthropic.Anthropic(); records the request and returns a canned reply."""

    def __init__(self, reply: dict, in_tok=1000, out_tok=200, stop_reason="end_turn"):
        self.calls = []
        self.reply, self.in_tok, self.out_tok, self.stop_reason = reply, in_tok, out_tok, stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.reply))],
                               usage=SimpleNamespace(input_tokens=self.in_tok, output_tokens=self.out_tok),
                               stop_reason=self.stop_reason, model="claude-opus-5-5")


HALLUCINATED = {"verdict": "request_changes", "summary": "claims a defect that does not exist", "findings": [{
    "severity": "blocking", "rule": "llm", "title": "Free shipping wrong for $80", "file": "shopcart/cart.py",
    "function": "shipping", "line": None, "detail": "claims cart of $80 pays shipping",
    "suggestion": "", "probe": ">>> from shopcart.cart import Cart\n>>> c = Cart(); c.add('x', 80.0)\n>>> c.shipping()\n0.0\n"}]}


def test_claude_adapter_request_shape_cost_and_refusal():
    fake = FakeClaudeClient({"verdict": "approve", "summary": "ok", "findings": []}, in_tok=10_000, out_tok=2_000)
    r = reviewers.ClaudeReviewer(client=fake, label_simulated=True)
    bundle = {"iteration": 1, "issue": {"title": "t", "body": "b"}, "prd": {}, "plan": {}, "diff": "", "tests": {},
              "previous_iterations": [], "files_after": {}}
    out = r.review(bundle, tools=None)
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"] and call["output_config"] == {"effort": "high"}
    assert out["cost_usd"] == pytest.approx((10_000 * 4 + 2_000 * 20) / 1e6)
    assert r.simulated is True  # a test double must never be reported as a real model
    with pytest.raises(reviewers.ReviewerError, match="declined"):
        reviewers.ClaudeReviewer(client=FakeClaudeClient({}, stop_reason="refusal")).review(bundle, None)


def test_claude_adapter_unavailable_without_key_and_approval(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert reviewers.ClaudeReviewer().available() == (False, "ANTHROPIC_API_KEY not set")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("SF_ALLOW_PAID_INFERENCE", raising=False)
    assert "paid inference not approved" in reviewers.ClaudeReviewer().available()[1]
    chosen, notes = reviewers.resolve(["claude", "simulated-diff"], 2)
    assert [c.name for c in chosen] == ["simulated-diff", "simulated-spec"]
    assert any("substituted simulated reviewer" in n for n in notes)


def test_command_adapter_runs_external_reviewer(tmp_path):
    script = tmp_path / "rev.py"
    script.write_text("import json, sys\nb = json.load(sys.stdin)\n"
                      "print(json.dumps({'verdict': 'approve', 'summary': 'saw iteration %d' % b['iteration'],"
                      " 'findings': [], 'input_tokens': 5, 'output_tokens': 1, 'cost_usd': 0.001}))\n")
    env = {"SF_REVIEWER_CMD_LOCAL": f'"{sys.executable}" "{script}"', "SF_REVIEWER_CMD_LOCAL_PROVIDER": "local"}
    r = reviewers.CommandReviewer("local", env=env)
    assert r.available()[0] and r.simulated  # assumed simulated unless explicitly declared real
    out = r.review({"iteration": 3}, None)
    assert out["summary"] == "saw iteration 3" and out["cost_usd"] == 0.001


def test_unconfirmed_llm_finding_is_downgraded_and_tokens_are_metered(orch, monkeypatch):
    monkeypatch.setitem(reviewers.FACTORIES, "fake-llm",
                        lambda: reviewers.ClaudeReviewer(client=FakeClaudeClient(HALLUCINATED), label_simulated=True))
    wf_id = submit_sample(orch, "shipping", options={"reviewers": ["simulated-spec", "simulated-diff", "fake-llm"]})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL  # the hallucinated blocker did not block the workflow
    it = orch.store.get_iterations(wf_id)[0]
    f = next(f for i in it["feedback"]["items"] for f in i["findings"] if f["reviewer"] == "claude")
    assert f["confirmed"] is False and f["severity"] == "major" and "unconfirmed" in f["detail"]
    assert wf["usage"]["llm_tokens"] == 1200 and wf["usage"]["cost_usd"] > 0
    assert [r["verdict"] for r in orch.store.get_reviews(wf_id, 1)][2] == "request_changes"


def test_cost_budget_blocks_and_escalates(orch, monkeypatch):
    monkeypatch.setitem(reviewers.FACTORIES, "fake-llm", lambda: reviewers.ClaudeReviewer(
        client=FakeClaudeClient({"verdict": "approve", "findings": []}, in_tok=100_000, out_tok=20_000),
        label_simulated=True))  # 100k*4 + 20k*20 per 1M = $0.80 per review
    wf_id = submit_sample(orch, "shipping", options={"reviewers": ["simulated-spec", "fake-llm"]},
                          budget={"max_cost_usd": 0.50})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.BLOCKED and "LLM budget exhausted: cost_usd" in wf["error"]
    assert orch.store.get_iterations(wf_id)[0]["status"] == IT.EXHAUSTED


def test_reviewers_receive_independent_copies(orch, monkeypatch):
    seen = {}

    class Mutator:
        name, provider, model, simulated, technique = "mutator", "test", "double", True, "mutates its input"
        def available(self): return True, ""
        def review(self, bundle, tools):
            bundle["tests"]["repro_passed"] = False
            bundle["diff"] = "tampered"
            return {"verdict": "approve", "findings": []}

    class Observer(Mutator):
        name = "observer"
        def review(self, bundle, tools):
            seen.update(repro=bundle["tests"]["repro_passed"], diff=bundle["diff"])
            return {"verdict": "approve", "findings": []}

    monkeypatch.setitem(reviewers.FACTORIES, "mutator", Mutator)
    monkeypatch.setitem(reviewers.FACTORIES, "observer", Observer)
    wf_id = submit_sample(orch, "shipping", options={"reviewers": ["mutator", "observer"]})
    run_all(orch)
    assert seen["repro"] is True and seen["diff"] != "tampered"
    assert orch.store.get_workflow(wf_id)["state"] == WF.AWAITING_APPROVAL


def test_reviewer_failure_is_never_treated_as_approval(orch, monkeypatch):
    class Broken:
        name, provider, model, simulated, technique = "broken", "test", "double", True, "always errors"
        def available(self): return True, ""
        def review(self, bundle, tools): raise RuntimeError("provider down")

    monkeypatch.setitem(reviewers.FACTORIES, "broken", Broken)
    wf_id = submit_sample(orch, "shipping", options={"reviewers": ["simulated-spec", "broken"]})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.FAILED and "only 1 of 2 reviewers completed" in wf["error"]
    assert orch.store.get_step(wf_id, "review")["attempts"] == 2  # retried once, then failed
    assert all(r["verdict"] in ("approve", "error") for r in orch.store.get_reviews(wf_id))


# ------------------------------------------------------------------ pure loop rules
BUDGET = {"max_iterations": 4, "stall_window": 2, "max_cost_usd": 1.0, "max_llm_tokens": 1000, "max_loop_seconds": 600}


def it(n, fp, sig, blocking):
    return {"n": n, "fingerprint": fp, "failure_signature": sig, "blocking_count": blocking}


@pytest.mark.parametrize("previous,current,usage,expected", [
    ([], it(1, "a", None, 0), {}, ("pass", "no blocking")),
    ([], it(1, "a", "s1", 2), {}, ("revise", "2 blocking")),
    ([it(1, "a", "s1", 2)], it(2, "a", "s2", 1), {}, ("stalled", "Repeated solution")),
    ([it(1, "a", "s1", 2)], it(2, "b", "s1", 2), {}, ("stalled", "Identical failure pattern")),
    ([it(1, "a", "s1", 1), it(2, "b", "s2", 1)], it(3, "c", "s3", 2), {}, ("stalled", "No progress")),
    # no progress is detected before the iteration limit is reported
    ([it(1, "a", "s1", 3), it(2, "b", "s2", 2), it(3, "c", "s3", 2)], it(4, "d", "s4", 2), {},
     ("stalled", "No progress")),
    ([it(1, "a", "s1", 4), it(2, "b", "s2", 3), it(3, "c", "s3", 2)], it(4, "d", "s4", 1), {},
     ("exhausted", "Iteration limit")),
    ([], it(1, "a", "s1", 1), {"cost_usd": 1.5}, ("exhausted", "Cost budget")),
    ([], it(1, "a", "s1", 1), {"llm_tokens": 5000}, ("exhausted", "Token budget")),
])
def test_assess_progress(previous, current, usage, expected):
    decision, reason = loop.assess_progress(previous, current, BUDGET, usage, loop_started_at=None)
    assert decision == expected[0] and expected[1] in reason


def test_loop_time_budget():
    d, reason = loop.assess_progress([], it(1, "a", "s", 1), BUDGET, {}, loop_started_at=0, now=10_000)
    assert d == "exhausted" and "time budget" in reason


def test_aggregate_groups_corroborates_and_signs():
    tests = {"repro_passed": True, "suite_passed": True, "probes": [], "security": {"findings": []}}
    reviews = [
        {"reviewer": "a", "verdict": "request_changes", "findings": [
            {"severity": "blocking", "rule": "doc", "title": "X wrong", "file": "f.py", "function": "g",
             "probe": ">>> 1\n2\n"}]},
        {"reviewer": "b", "verdict": "approve", "findings": [
            {"severity": "major", "rule": "pattern", "title": "same pattern", "file": "f.py", "function": "g", "line": 7}]},
        {"reviewer": "c", "verdict": "error", "findings": [{"severity": "blocking", "title": "ignored"}]},
    ]
    mismatch = "Failed example:\n    1\nExpected:\n    2\nGot:\n    1\n"
    agg = loop.aggregate(reviews, tests, confirm_probe=lambda src: (False, mismatch))
    assert agg["blocking_count"] == 1 and len(agg["items"]) == 1
    item = agg["items"][0]
    assert item["corroborated"] and item["sources"] == ["a", "b"] and item["location"]["lines"] == [7]
    assert agg["probes"] == [">>> 1\n2\n"] and agg["focus"]["lines"] == {"f.py": [7]}
    assert agg["signature"] and agg["strategy"] == "build_on_previous"

    failing = loop.aggregate([], {**tests, "suite_passed": False, "suite_summary": "1 failed"}, lambda s: (True, ""))
    assert failing["blocking_count"] == 1 and failing["strategy"] == "restart_from_checkpoint"


def test_iteration_state_machine():
    check_iteration_transition(IT.CODING, IT.TESTING)
    check_iteration_transition(IT.AGGREGATING, IT.REVISE)
    check_iteration_transition(IT.TESTING, IT.TESTING)  # re-entering the same phase on a retried attempt
    for bad in [(IT.CODING, IT.PASSED), (IT.TESTING, IT.AGGREGATING), (IT.PASSED, IT.CODING), (IT.REVISE, IT.TESTING)]:
        with pytest.raises(IllegalTransition):
            check_iteration_transition(*bad)


def test_unified_diff_parser():
    hunks = loop.parse_unified_diff("diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -10 +10 @@ def g():\n"
                                    "-    if x > 5:\n+    if x >= 5:\n")
    assert hunks == [{"file": "f.py", "removed": [(10, "    if x > 5:")], "added": [(10, "    if x >= 5:")]}]
