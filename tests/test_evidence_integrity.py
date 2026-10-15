"""Release check for committed evidence packages (examples/evidence/**).

Every committed package must have been produced by a clean checkout of a System Factory commit that is part of
this repository's history. Catches stale revisions (e.g. after a history rewrite) and dirty-tree runs.
Requires full git history (CI uses fetch-depth: 0).
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = sorted(ROOT.glob("examples/evidence/**/run-manifest.json"))
REQUIRED = {"summary.md", "run-manifest.json", "iterations.json", "review-findings.json", "test-results.json",
            "quality-gate.json", "patch.diff", "llm-calls.json", "tool-calls.json", "events.json"}


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def test_there_is_committed_evidence():
    assert MANIFESTS, "no evidence packages found under examples/evidence"


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.parent.name)
def test_evidence_comes_from_a_clean_reachable_commit(manifest):
    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        pytest.fail("shallow clone: evidence provenance needs full history (fetch-depth: 0)")
    m = json.loads(manifest.read_text(encoding="utf-8"))
    run = m["system_factory"]["run"]
    rev = run.get("source_revision")
    assert rev and re.fullmatch(r"[0-9a-f]{40}", rev), f"{manifest}: no source revision recorded"
    assert git("cat-file", "-e", f"{rev}^{{commit}}").returncode == 0, f"{manifest}: {rev[:10]} does not exist"
    assert git("merge-base", "--is-ancestor", rev, "HEAD").returncode == 0, \
        f"{manifest}: {rev[:10]} is not in the history of HEAD (stale revision)"
    assert run["source_dirty"] is False, f"{manifest}: produced from a dirty checkout"
    assert m["system_factory"]["export"]["source_dirty"] is False, f"{manifest}: exported from a dirty checkout"


@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.parent.name)
def test_evidence_package_is_complete_valid_and_sanitized(manifest):
    pkg = manifest.parent
    assert REQUIRED <= {p.name for p in pkg.iterdir()}
    blob = ""
    for p in pkg.iterdir():
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            json.loads(text)
        blob += text
    for leak in (r"[A-Za-z]:[\\/]+Users[\\/]", r"/home/[a-z]", r"sk-ant-[A-Za-z0-9_-]{10,}", r"gh[pousr]_[A-Za-z0-9]{20,}"):
        assert not re.search(leak, blob), f"{pkg.name}: matches {leak}"
    m = json.loads(manifest.read_text(encoding="utf-8"))
    calls = json.loads((pkg / "llm-calls.json").read_text(encoding="utf-8"))
    if "simulated" in m["execution_modes"] and m["totals"]["llm_calls"] == 0:
        assert calls == []
    for c in calls:  # real model calls must carry genuine provider records
        assert c["provider"] and c["model"] and c["execution"] in ("real-local", "real-cloud", "simulated")
        if c["status"] == "ok" and c["execution"] == "real-local":
            assert c["model_digest"] and c["input_tokens"] is not None and c["output_tokens"] is not None
