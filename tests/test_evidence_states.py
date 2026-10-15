"""Milestone 6 evidence hardening: a failing reviewer check is a confirmed defect only when its expectation is
justified (grounded in a requirement or corroborated); otherwise the state is explicit and escalates."""
import subprocess
import time

import pytest

from systemfactory import loop, reviewers, roles
from systemfactory.context import PermanentError, StepContext
from systemfactory.statemachine import IT, WF

from .conftest import run_all, submit_sample

TESTS_GREEN = {"repro_passed": True, "suite_passed": True, "probes": [], "security": {"findings": []}}
REQS = ["Orders of $50.00 or more ship free.", "Banner shown at checkout.",
        ">>> shipping_banner(80.0)\n'Your order ships free!'"]
MISMATCH = "Failed example:\n    f(1)\nExpected:\n    'X'\nGot:\n    'Y'\n"


def check(reviewer, call, expected, rule="", fn="shipping_banner"):
    return {"reviewer": reviewer, "verdict": "approve", "simulated": False, "findings": [], "checks": [
        {"function": fn, "call": call, "expected": expected, "rule": rule, "file": "shopcart/checkout.py",
         "probe": f">>> from shopcart.checkout import {fn}\n>>> {call}\n{expected}\n"}]}


def agg(reviews, outcome=(False, MISMATCH), requirements=REQS):
    return loop.aggregate(reviews, TESTS_GREEN, lambda src: outcome, requirements=requirements)


def states(a):
    return [c.get("evidence_state") for c in a["checks"]]


def test_failing_check_grounded_in_requirement_is_a_confirmed_defect():
    a = agg([check("rev-a", "shipping_banner(50.0)", "'Your order ships free!'",
                   rule="Orders of $50.00 or more ship free")])
    assert states(a) == ["confirmed"] and a["blocking_count"] == 1 and a["unresolved_count"] == 0
    assert a["checks"][0]["grounding"] in loop.requirement_sentences(REQS)  # justified by an authoritative sentence
    b = agg([check("rev-a", "shipping_banner(50.0)", "'Ships free'", rule="Orders of $50.00 or more ship free")])
    assert states(b) == ["confirmed"] and b["checks"][0]["grounding"] == "Orders of $50.00 or more ship free."


def test_value_coincidence_is_not_grounding():
    # a substring of a different documented message, and a bare number, justify nothing on their own
    assert loop.ground("", "'ships free'", loop.requirement_sentences(REQS)) is None
    assert loop.ground("", "0.0", ["shipping_cost(80.0) returns 0.0"]) is None
    assert loop.ground("", "'Your order ships free!'", loop.requirement_sentences(REQS)) is not None


def test_failing_check_with_unjustified_expectation_is_unresolved_not_blocking():
    a = agg([check("rev-a", "shipping_banner(50.0)", "'Free delivery!'", rule="banners should be exciting")])
    assert states(a) == ["unsupported"] and a["blocking_count"] == 0 and a["unresolved_count"] == 1
    decision, reason = loop.assess_progress([], {"n": 1, "fingerprint": "x", "failure_signature": None,
                                                 "blocking_count": 0, "unresolved_count": 1}, {}, {}, None)
    assert decision == "unresolved" and "unsupported or conflicting" in reason


def test_identical_expectation_from_two_reviewers_is_corroborated():
    a = agg([check("rev-a", "shipping_banner(51.0)", "'Free delivery!'"),
             check("rev-b", "shipping_banner(51.0)", "'Free delivery!'")])
    assert states(a) == ["confirmed", "confirmed"] and a["blocking_count"] == 1
    assert a["checks"][0]["corroborated_by"] == ["rev-b"]


def test_conflicting_expectations_are_unresolved():
    a = agg([check("rev-a", "shipping_banner(50.0)", "'Your order ships free!'", rule="Orders of $50.00 or more ship free"),
             check("rev-b", "shipping_banner(50.0)", "'Add $0.00 more for free shipping'", rule="Orders of $50.00 or more ship free")])
    assert states(a) == ["conflicting", "conflicting"] and a["blocking_count"] == 0 and a["unresolved_count"] == 1


def test_passing_and_invalid_checks_are_recorded_but_never_block():
    r = check("rev-a", "shipping_banner(80.0)", "'Your order ships free!'")
    r["checks"].append({"function": "shipping_banner", "call": "open('x')", "expected": "1", "probe": None,
                        "invalid_reason": "call is not a direct call of shipping_banner()"})
    a = agg([r], outcome=(True, "ok"))
    assert states(a) == ["passed", "invalid"] and a["blocking_count"] == 0 and a["unresolved_count"] == 0


def test_ambiguous_function_names_are_invalid_unless_qualified():
    inv = reviewers.function_inventory({"a/x.py": "def total(n):\n    return n\n", "b/y.py": "def total(n):\n    return n\n"})
    probe, why = reviewers.validate_check({"function": "total", "call": "total(1)", "expected": "1"}, inv)
    assert probe is None and "ambiguous" in why
    probe, why = reviewers.validate_check({"function": "b.y.total", "call": "total(1)", "expected": "1"}, inv)
    assert why is None and probe.startswith(">>> from b.y import total")


def test_model_probe_confirmed_but_ungrounded_is_unresolved():
    r = {"reviewer": "rev-a", "verdict": "request_changes", "simulated": False, "checks": [], "findings": [{
        "severity": "blocking", "title": "wrong colour", "detail": "should be purple", "file": "f.py",
        "function": "g", "probe": ">>> from f import g\n>>> g()\n'purple'\n"}]}
    a = agg([r])
    f = a["items"][0]["findings"][0]
    assert f["evidence_state"] == "unsupported" and f["severity"] == "unresolved" and a["blocking_count"] == 0


def test_approval_without_inspection_does_not_pass():
    d, why = loop.assess_progress([], {"n": 1, "fingerprint": "x", "failure_signature": None, "blocking_count": 0,
                                       "unresolved_count": 0, "inspected": 1, "min_reviewers": 2}, {}, {}, None)
    assert d == "unresolved" and "without inspection" in why
    a = agg([{"reviewer": "lazy", "verdict": "approve", "simulated": False, "findings": [], "checks": []}])
    assert a["inspected_reviews"] == {}


def test_reviewers_cannot_assign_internal_severities():
    r = {"reviewer": "rev-a", "verdict": "approve", "simulated": False, "checks": [],
         "findings": [{"severity": "unresolved", "title": "t", "file": "f.py", "function": "g"}]}
    assert agg([r])["items"][0]["severity"] == "major"


def test_unresolved_evidence_escalates_the_workflow(orch, monkeypatch):
    class Overconfident:
        """Labelled test double: a reviewer whose expectation is not in any requirement."""
        name, provider, model, simulated, technique = "overconfident", "test", "double", True, "invents a spec"
        def available(self): return True, ""
        def review(self, bundle, tools):
            return {"verdict": "request_changes", "findings": [], "checks": [{
                "function": "shipping", "call": "shipping()", "expected": "123.0", "rule": "the moon is made of cheese",
                "probe": ">>> from shopcart.cart import Cart\n>>> Cart().shipping()\n123.0\n", "file": "shopcart/cart.py"}]}

    monkeypatch.setitem(reviewers.FACTORIES, "overconfident", Overconfident)
    wf_id = submit_sample(orch, "shipping", options={"reviewers": ["simulated-spec", "simulated-diff", "overconfident"]})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    it = orch.store.get_iterations(wf_id)[-1]
    assert wf["state"] == WF.BLOCKED and it["status"] == IT.UNRESOLVED
    assert wf["data"]["escalation"]["decision"] == "unresolved"
    assert any(b["severity"] == "unresolved" for b in wf["data"]["escalation"]["open_blocking"])


def test_gate_refuses_a_commit_that_was_not_reviewed(orch):
    wf_id = submit_sample(orch, "shipping")
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    (orch.settings.worktrees_dir / f"wf-{wf_id}" / "NOTES.md").write_text("added after review\n", encoding="utf-8")
    orch.git.run(wf["worktree"], "add", "NOTES.md")
    orch.git.run(wf["worktree"], "commit", "-qm", "unreviewed change")
    ctx = StepContext(orch.settings, orch.store, orch.git, wf_id, "quality_gate", "quality-gate", 1, time.time() + 60)
    with pytest.raises(PermanentError, match="reviewed_commit_is_final"):
        roles.quality_gate(ctx)


def test_provenance_is_recorded_at_submission(orch):
    wf_id = submit_sample(orch, "shipping")
    prov = orch.store.get_workflow(wf_id)["data"]["provenance"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert prov["source_revision"] == head and isinstance(prov["source_dirty"], bool)


def test_demo_base_commit_is_reproducible(tmp_path):
    from systemfactory.config import Settings
    from systemfactory.demo import init_demo_repo
    from systemfactory.sandbox import Git
    shas = []
    for name in ("one", "two"):
        s = Settings(data_dir=tmp_path / name)
        init_demo_repo(s)
        shas.append(Git(s.empty_gitconfig).out(s.demo_repo, "rev-parse", "HEAD"))
    assert shas[0] == shas[1]
