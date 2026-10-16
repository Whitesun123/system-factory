"""Regressions for the Milestone 6 independent code review (fresh-context review by the same model family: a second
pair of eyes, not model diversity). Each test names the finding it pins down."""
import doctest

import pytest

from evaluation import harness
from systemfactory import execution, loop, reviewers
from systemfactory.config import Settings

from .test_evidence_states import TESTS_GREEN, _real_probe_runner, states

REQS = ["Template names containing '..' or '/' raise ValueError('invalid template name')."]
INV = [{"function": "template_path", "module": "tplpkg.templates"}]


def _review(call, expected, reviewer="rev-a", rule=""):
    c = {"function": "template_path", "call": call, "expected": expected, "rule": rule, "file": "tplpkg/templates.py"}
    probe, why = reviewers.validate_check(c, INV)
    assert why is None
    return {"reviewer": reviewer, "verdict": "approve", "simulated": False, "findings": [],
            "checks": [{**c, "probe": probe}]}


# R1: a check that hangs or is killed on the patched code was dropped as "invalid" and still counted as inspection
@pytest.mark.parametrize("killed", ["[killed after 35.0s timeout]", "[container killed: exit 137 (out of memory or SIGKILL)]"])
def test_killed_check_is_a_failure_not_invalid(killed):
    assert loop.classify_probe(False, "partial output\n" + killed) == "timeout"
    a = loop.aggregate([_review("template_path('../x')", "ValueError('invalid template name')",
                                rule="Template names containing '..' raise ValueError")], TESTS_GREEN,
                       lambda src: (False, killed), requirements=REQS, changed_functions={"template_path"},
                       changed_files={"tplpkg/templates.py"})
    assert states(a) == ["confirmed"] and a["blocking_count"] == 1  # grounded expectation, code never finished


def test_killed_ungrounded_check_escalates_instead_of_passing():
    a = loop.aggregate([_review("template_path('x')", "'templates/x.txt'")], TESTS_GREEN,
                       lambda src: (False, "[killed after 35.0s timeout]"), requirements=["unrelated text"],
                       changed_functions={"template_path"}, changed_files={"tplpkg/templates.py"})
    assert a["blocking_count"] + a["unresolved_count"] >= 1


# R2: a TypeError/NameError raised INSIDE the patched code was classified as a broken probe
def test_setup_error_raised_inside_the_code_under_test_is_a_real_exception(tmp_path):
    run = _real_probe_runner(tmp_path, "def template_path(name):\n    if name.startswith('-'):\n        return len(None)\n"
                                       "    return 'templates/' + name + '.txt'\n")
    probe = _review("template_path('-x')", "'templates/-x.txt'")["checks"][0]["probe"]
    ok, out = run(probe)
    assert loop.classify_probe(ok, out) == "exception"
    a = loop.aggregate([_review("template_path('-x')", "'templates/-x.txt'")], TESTS_GREEN, run,
                       requirements=["template_path('-x') returns 'templates/-x.txt'"],
                       changed_functions={"template_path"}, changed_files={"tplpkg/templates.py"})
    assert a["blocking_count"] + a["unresolved_count"] == 1


def test_wrong_signature_in_the_probe_itself_is_still_a_probe_error(tmp_path):
    run = _real_probe_runner(tmp_path, "def template_path(name):\n    return 'templates/' + name + '.txt'\n")
    ok, out = run(">>> from tplpkg.templates import template_path\n>>> template_path('a', 'b')\n'x'\n")
    assert loop.classify_probe(ok, out) == "probe-error"
    a = loop.aggregate([{"reviewer": "rev-a", "verdict": "approve", "simulated": False, "findings": [], "checks": [
        {"function": "template_path", "call": "template_path('a', 'b')", "expected": "'x'",
         "probe": ">>> from tplpkg.templates import template_path\n>>> template_path('a', 'b')\n'x'\n"}]}],
        TESTS_GREEN, run, requirements=REQS, changed_functions={"template_path"},
        changed_files={"tplpkg/templates.py"})
    assert "rev-a" not in a["inspected_reviews"]  # a broken probe inspected nothing


# R3: a "located" finding in a file the patch did not change counted as an inspection
def test_located_finding_must_name_a_changed_file_or_function():
    fake = {"reviewer": "rev-b", "verdict": "approve", "simulated": False, "checks": [],
            "findings": [{"severity": "minor", "title": "t", "file": "nonexistent.py", "function": "nope"}]}
    real = {**fake, "reviewer": "rev-c", "findings": [{"severity": "minor", "title": "t", "file": "tplpkg/templates.py"}]}
    a = loop.aggregate([fake, real], TESTS_GREEN, lambda s: (True, "ok"), requirements=REQS,
                       changed_functions={"template_path"}, changed_files={"tplpkg/templates.py"})
    assert "rev-b" not in a["inspected_reviews"] and "rev-c" in a["inspected_reviews"]


# R4: `from <changed module> import os` and str.format passed the free-form probe vetting
def test_probe_may_import_only_what_the_changed_module_defines():
    files = {"shopkit/templates.py": "import os\n\ndef template_path(name):\n    return os.path.join('t', name)\n"}
    allowed = reviewers.module_definitions(files)
    assert allowed == {"shopkit.templates": {"template_path"}}
    bad = ">>> from shopkit.templates import os\n>>> os.system('echo synthetic')\n0\n"
    assert not reviewers.probe_is_safe(bad, allowed)
    assert not reviewers.probe_is_safe(">>> from shopkit.templates import template_path\n"
                                       ">>> '{0.__globals__}'.format(template_path)\n''\n", allowed)
    assert reviewers.probe_is_safe(">>> from shopkit.templates import template_path\n>>> template_path('a')\n't/a'\n",
                                   allowed)
    vetted = reviewers.vet_findings([{"title": "t", "probe": bad}], files)
    assert vetted[0]["probe"] is None and "rejected" in vetted[0]["detail"]


# R5: the evaluation harness ran with --execution docker even when Docker was unavailable
def test_evaluation_fails_closed_without_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(execution.DockerExecutionBackend, "available", lambda self: (False, "daemon not running"))
    s = Settings(data_dir=tmp_path / "d")
    s.ensure_dirs()
    with pytest.raises(execution.IsolationUnavailable):
        harness.Materialised(harness.load_case("bulk-partial"), tmp_path / "w", s.empty_gitconfig, "docker")


# R6: the raw-verdict baseline counted a reviewer error as an approval
def test_reviewer_error_is_neither_approval_nor_block_in_the_raw_baseline():
    d = harness.decide([{"reviewer": "ollama-a", "verdict": "error"}], {"issue": {}}, None, needed=1)
    assert d == {"raw": "error", "pipeline": "error", "reason": "reviewer error"}
    m = harness.metrics([{"expected_blocking": True, **d}], "raw")
    assert m["false_approvals"] == 0 and m["errors"] == 1


# R7: documentation rewritten by the patch was treated as the specification
def test_spec_reviewer_checks_against_the_docstring_before_the_patch():
    before = 'def f(x):\n    """\n    >>> f(1)\n    2\n    """\n    return x + 1\n'
    after = 'def f(x):\n    """\n    >>> f(1)\n    1\n    """\n    return x\n'  # patch "documents" its own bug
    probes = []

    class Tools:
        def run_probe(self, probe):
            probes.append(probe)
            return False, "Expected:\n    2\nGot:\n    1\n"
    bundle = {"tests": {}, "files_after": {"m.py": after}, "files_before": {"m.py": before},
              "diff_u0": "+++ b/m.py\n@@ -6 +6 @@\n-    return x + 1\n+    return x\n"}
    res = reviewers.SpecConformanceReviewer().review(bundle, Tools())
    assert probes and probes[0].endswith(">>> f(1)\n2\n") and res["verdict"] == "request_changes"


def test_grounding_in_the_evaluation_uses_pre_patch_documentation(tmp_path):
    # an expectation that only the PATCHED docstring supports is not grounded
    before = {"tplpkg/templates.py": 'def template_path(name):\n    """Return templates/<name>.txt."""\n'}
    after = {"tplpkg/templates.py": 'def template_path(name):\n    """Names starting with x raise ValueError(\'nope x\')."""\n'}
    run = _real_probe_runner(tmp_path, "def template_path(name):\n    return 'templates/' + name + '.txt'\n")
    rev = _review("template_path('xa')", "ValueError('nope x')")
    bundle = {"issue": {"title": "t", "body": "b"}, "prd": {}, "files_after": after, "tests": TESTS_GREEN}
    d_before = harness.decide([rev], bundle, run, needed=1, files_before=before)
    assert d_before["pipeline"] == "escalate" and d_before["check_states"]["unsupported"] == 1


# R8 (small): host-path translation must respect path-component boundaries
def test_docker_path_translation_respects_component_boundaries(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    argv = execution.DockerExecutionBackend(docker="docker").argv(
        [str(work / "a.py"), str(tmp_path / "workother" / "b.py")], workdir=work, ro_dirs={}, name="n")
    assert argv[-2] == "/work/a.py" and argv[-1] == str(tmp_path / "workother" / "b.py")


def test_exception_message_probe_has_exactly_two_examples():
    probe, _ = reviewers.validate_check({"function": "template_path", "call": "template_path('a')",
                                         "expected": "ValueError('x')"}, INV)
    assert len(doctest.DocTestParser().get_examples(probe)) == 2
