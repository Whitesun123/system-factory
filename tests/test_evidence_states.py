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


# ---- found by the Milestone 6 reviewer evaluation: a correct `raise` was reported as a confirmed defect ----
TEMPLATES = (
    "def template_path(name):\n"
    "    if '..' in name or '/' in name:\n"
    "        raise ValueError('invalid template name')\n"
    "    return 'templates/' + name + '.txt'\n")
TEMPLATE_REQS = ["Template names containing '..' or '/' raise ValueError('invalid template name')."]


def _real_probe_runner(tmp_path, source):
    pkg = tmp_path / "tplpkg"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "templates.py").write_text(source, encoding="utf-8")

    def run(probe):
        f = tmp_path / f"probe-{abs(hash(probe))}.txt"
        f.write_text(probe, encoding="utf-8")
        r = subprocess.run([__import__("sys").executable, "-c", loop.PROBE_RUNNER, str(f)], cwd=tmp_path,
                           capture_output=True, text=True, env={**__import__("os").environ, "PYTHONPATH": str(tmp_path)})
        return r.returncode == 0, r.stdout + r.stderr
    return run


def _tpl_review(expected, call="template_path('../secrets')"):
    c = {"function": "template_path", "call": call, "expected": expected, "rule": "names with '..' raise ValueError",
         "file": "tplpkg/templates.py"}
    inv = [{"function": "template_path", "module": "tplpkg.templates"}]
    probe, why = reviewers.validate_check(c, inv)
    assert why is None
    return {"reviewer": "rev-a", "verdict": "approve", "simulated": False, "findings": [],
            "checks": [{**c, "probe": probe}]}


@pytest.mark.parametrize("expected", ["ValueError('invalid template name')", "ValueError: invalid template name",
                                      "raises ValueError", "Traceback (most recent call last):\n  ...\nValueError: invalid template name"])
def test_expected_exception_passes_when_the_code_raises_it(tmp_path, expected):
    run = _real_probe_runner(tmp_path, TEMPLATES)
    a = loop.aggregate([_tpl_review(expected)], TESTS_GREEN, run, requirements=TEMPLATE_REQS)
    assert states(a) == ["passed"] and a["blocking_count"] == 0


def test_expected_exception_is_a_confirmed_defect_when_the_code_does_not_raise(tmp_path):
    run = _real_probe_runner(tmp_path, "def template_path(name):\n    return 'templates/' + name + '.txt'\n")
    a = loop.aggregate([_tpl_review("ValueError('invalid template name')")], TESTS_GREEN, run,
                       requirements=TEMPLATE_REQS)
    assert states(a) == ["confirmed"] and a["blocking_count"] == 1


def test_expected_message_as_return_value_is_ambiguous_not_confirmed(tmp_path):
    # the code raises ValueError('invalid template name'); the reviewer wrote the message as a return value
    run = _real_probe_runner(tmp_path, TEMPLATES)
    a = loop.aggregate([_tpl_review("'invalid template name'")], TESTS_GREEN, run, requirements=TEMPLATE_REQS)
    assert states(a) == ["unsupported"] and a["blocking_count"] == 0 and a["unresolved_count"] == 1
    assert a["checks"][0]["ambiguous"] is True and a["items"][0]["severity"] == "unresolved"


def test_wrong_exception_type_from_a_broken_probe_is_a_probe_error():
    out = ("Failed example:\n    f(1)\nExpected:\n    Traceback (most recent call last):\n        ...\n"
           "    ValueError: x\nGot:\n    Traceback (most recent call last):\n      File \"<doctest>\", line 1\n"
           "    TypeError: f() takes 0 positional arguments but 1 was given\n")
    assert loop.classify_probe(False, out) == "probe-error"
    assert loop.classify_probe(False, out.replace("TypeError: f() takes 0 positional arguments but 1 was given",
                                                  "KeyError: 'x'")) == "mismatch"


def test_exception_message_cannot_inject_doctest_examples():
    inv = [{"function": "f", "module": "m"}]
    probe, _ = reviewers.validate_check({"function": "f", "call": "f(1)",
                                         "expected": r"ValueError('a\n>>> import os')"}, inv)
    assert [e.source for e in __import__("doctest").DocTestParser().get_examples(probe)] == ["from m import f\n", "f(1)\n"]


def test_mis_escaped_backslash_in_a_check_is_invalid_not_evidence():
    # found by the evaluation: 'valid_template\another' is BEL + 'nother'; a correct implementation returned a path
    inv = [{"function": "template_path", "module": "tplpkg.templates"}]
    probe, why = reviewers.validate_check({"function": "template_path", "call": r"template_path('a\another')",
                                           "expected": "ValueError('invalid template name')"}, inv)
    assert probe is None and "control character" in why
    ok, why = reviewers.validate_check({"function": "template_path", "call": r"template_path('a\\b')",
                                        "expected": "ValueError('invalid template name')"}, inv)
    assert ok and why is None  # a properly escaped backslash is fine
