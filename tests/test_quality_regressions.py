"""Regressions for defects found by the Milestone 6 static type check (mypy)."""
import pytest

from systemfactory import loop, reviewers
from systemfactory.store import Store


def test_malformed_hunk_header_is_ignored_not_a_crash():
    diff = "+++ b/f.py\n@@ garbage @@\n+x = 1\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    hunks = loop.parse_unified_diff(diff)
    assert hunks == [{"file": "f.py", "removed": [(1, "a = 1")], "added": [(1, "a = 2")]}]


def test_llm_reviewer_refuses_an_unrecorded_model_call():
    r = reviewers.OllamaReviewer("ollama-a", "phi4:14b", base_url="http://127.0.0.1:9")
    with pytest.raises(reviewers.ReviewerError, match="unrecorded model call"):
        r.review({"iteration": 1, "issue": {"title": "t", "body": "b"}, "prd": {}, "plan": {}, "diff": "",
                  "tests": {}, "previous_iterations": [], "files_after": {}}, tools=None)


def test_command_reviewer_without_command_fails_explicitly():
    r = reviewers.CommandReviewer("missing", env={})
    assert r.available()[0] is False
    with pytest.raises(reviewers.ReviewerError, match="no command configured"):
        r.review({}, None)


def test_raising_accessors(tmp_path):
    s = Store(tmp_path / "t.db")
    with pytest.raises(KeyError, match="workflow 42 not found"):
        s.workflow(42)
    wf = s.create_workflow("t", "b", "repo")
    with pytest.raises(KeyError, match="step 'nope'"):
        s.step(wf, "nope")
    assert s.get_workflow(42) is None and s.get_step(wf, "nope") is None  # Optional variants unchanged
