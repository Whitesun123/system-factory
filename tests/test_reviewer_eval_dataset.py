"""The reviewer evaluation dataset is itself tested: every label is checked against its oracle, every case is a valid
bug-fix (reproduction test fails before, passes after; existing tests pass), and the deterministic baseline runs
through the real decision pipeline. No model is called here."""
import pytest

from evaluation import harness
from evaluation.dataset import CASE_IDS
from systemfactory.config import Settings

LABELS = harness.LABELS


def test_dataset_size_and_balance():
    assert len(CASE_IDS) >= 20 and set(CASE_IDS) == set(LABELS["cases"])
    blocking = sum(LABELS["cases"][c]["expected_blocking"] for c in CASE_IDS)
    assert 9 <= blocking <= len(CASE_IDS) - 9  # both outcomes well represented
    cats = {LABELS["cases"][c]["category"] for c in CASE_IDS}
    for required in ("boundary condition", "missing functionality", "incorrect boolean logic", "regression",
                     "incomplete fix across related functions", "exception handling", "data validation",
                     "security-sensitive change", "correct change"):
        assert required in cats


def test_labels_never_reach_reviewers():
    case = harness.load_case(CASE_IDS[0])
    blob = repr({k: case[k] for k in ("issue", "before", "after", "tests")})
    assert "expected_blocking" not in blob and "oracle" not in blob


@pytest.fixture(scope="module")
def gitconfig(tmp_path_factory):
    s = Settings(data_dir=tmp_path_factory.mktemp("evaldata"))
    s.ensure_dirs()
    return s.empty_gitconfig


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_case_is_a_valid_bug_fix_and_label_matches_oracle(case_id, tmp_path, gitconfig):
    case = harness.load_case(case_id)
    m = harness.Materialised(case, tmp_path, gitconfig)
    assert not m.pytest("tests/test_issue_1.py").ok, "reproduction test must fail before the patch"
    assert not m.oracle_passes(), "the unpatched code must violate the specification"
    m.apply_patch()
    assert m.pytest("tests/test_issue_1.py").ok, "the patch must fix the reported symptom"
    assert m.pytest().ok, "existing tests must still pass (the defects are not covered by tests)"
    assert m.oracle_passes() is (not case["expected_blocking"]), "label disagrees with the oracle"


def test_deterministic_baseline_through_the_pipeline(tmp_path, gitconfig):
    case = harness.load_case("checkout-partial")
    m = harness.Materialised(case, tmp_path, gitconfig)
    m.apply_patch()
    b = m.bundle()
    revs = [harness.run_reviewer(n, None, b, m) for n in ("simulated-spec", "simulated-diff")]
    d = harness.decide(revs, b, m.run_probe, needed=2)
    assert d["pipeline"] in ("approve", "block", "escalate") and d["inspected"] == 2


def test_metrics_definitions():
    rows = [{"expected_blocking": True, "m": "block"}, {"expected_blocking": True, "m": "approve"},
            {"expected_blocking": False, "m": "block"}, {"expected_blocking": False, "m": "escalate"}]
    m = harness.metrics(rows, "m")
    assert (m["true_positives"], m["false_approvals"], m["false_blocks"], m["escalations"]) == (1, 1, 1, 1)
    assert m["precision"] == 0.5 and m["recall"] == 0.5


def test_report_renderer_uses_only_recorded_numbers_and_lists_unexecuted_pairs():
    from evaluation.report_md import render
    dec = {"raw": "approve", "pipeline": "escalate"}
    fixture = {  # test fixture, not a result: shape of evaluation/results/report.json
        "dataset_cases": 2, "executed_cases": 2, "configs": ["ollama-a=m1"], "model_digests": {"m1": "abc"},
        "prompt_hashes": {"ollama-a": "p"}, "budget_minutes": 1, "wall_minutes": 0.2,
        "inference_minutes": 0.1, "new_inference_minutes_this_run": 0.0, "execution_backend": "local", "not_executed": [{"case": "c2", "config": "ollama-a=m1"}],
        "summary": {"ollama-a=m1": {"raw": {}, "pipeline": {"cases": 1}, "latency_s": {"mean": 1.0, "p95": 1.0},
                                    "tokens": {"input_mean": 10, "output_mean": 5}, "errors": 0}},
        "cases": [{"case": "c1", "category": "x", "expected_blocking": True,
                   "reviews": {"ollama-a=m1": {"verdict": "approve"}}, "decisions": {"ollama-a=m1": dec}},
                  {"case": "c2", "category": "x", "expected_blocking": False, "reviews": {}, "decisions": {}}]}
    md = render(fixture)
    assert "Comparable subset: 1 cases" in md
    assert "| c2 | x | approve | not run |" in md
    assert "- `ollama-a=m1`: c2" in md and "one provider" in md


def test_inference_budget_is_cumulative_across_runs():
    from evaluation.run import prior_inference_seconds
    cache = {"k1": {"role": "ollama-a", "model": "m1", "digest": "d1", "case": "c1", "review": {"seconds": 1000}},
             "k2": {"role": "ollama-a", "model": "m1", "digest": "OLD", "case": "c1", "review": {"seconds": 50}},
             "k3": {"role": "ollama-b", "model": "m2", "digest": "d2", "case": "c2", "review": {"seconds": 900}}}
    assert prior_inference_seconds(cache, [("ollama-a", "m1", "d1"), ("ollama-b", "m2", "d2")], ["c1", "c2"]) == 1900
    assert prior_inference_seconds(cache, [("ollama-a", "m1", "d1")], ["c2"]) == 0  # other cases do not count


def test_cached_reviews_are_rescored_with_current_check_validation():
    bundle = {"files_after": {"shopcart/checkout.py": "def shipping_cost(total):\n    return 0.0\n"}}
    stale = {"reviewer": "ollama-a", "checks": [{"function": "shipping_cost", "call": "shipping_cost(-1)",
                                                 "expected": "ValueError('negative')", "probe": "STALE", "valid": True}]}
    fresh = harness.revalidate(stale, bundle)
    assert "Traceback (most recent call last)" in fresh["checks"][0]["probe"] and stale["checks"][0]["probe"] == "STALE"
