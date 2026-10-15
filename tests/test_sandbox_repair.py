import sys

import pytest

from systemfactory import repair
from systemfactory.sandbox import Cancelled, Workspace, run_command, scrubbed_env


def test_scrubbed_env_drops_secrets_and_unknown_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("SOME_RANDOM_VAR", "x")
    env = scrubbed_env()
    assert "ANTHROPIC_API_KEY" not in env and "GITHUB_TOKEN" not in env and "SOME_RANDOM_VAR" not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    res = run_command([sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY'))"],
                      ".", timeout=20, env=env)
    assert res.output.strip() == "None"


def test_workspace_blocks_escape_git_and_unlisted_writes(tmp_path):
    (tmp_path / "src").mkdir()
    ws = Workspace(tmp_path, writable=["src/*.py"])
    ws.write_text("src/a.py", "x = 1\n")
    for bad in ["../outside.py", "tests/test_a.py", ".git/config", "src/../../evil.py"]:
        with pytest.raises(PermissionError):
            ws.write_text(bad, "boom")
    with pytest.raises(PermissionError):
        ws.read_text("../../etc/passwd")


def test_run_command_timeout_and_cancel():
    env = scrubbed_env()
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    res = run_command(slow, ".", timeout=0.5, env=env)
    assert res.timed_out and not res.ok

    def stop():
        raise Cancelled("stop")
    with pytest.raises(Cancelled):
        run_command(slow, ".", timeout=30, env=env, should_stop=stop)


SRC = '''def shipping(total):
    if total > 50:
        return 0
    return 5


def unrelated(x):
    return x + 1
'''


def test_localisation_prefers_functions_named_in_example():
    ids, mods = repair.identifiers_in_example(">>> from shop.cart import shipping\n>>> shipping(50)\n0\n")
    assert "shipping" in ids and mods == {"shop.cart"}
    assert repair.line_ranges(SRC, ids, "focus") == [(1, 4)]


def test_candidates_are_valid_python_and_include_the_fix():
    cands = list(repair.generate_candidates("f.py", SRC, [(1, 4)], ["compare"]))
    assert [c.description for c in cands] == ["line 2: `>` -> `>=`"]
    assert "total >= 50" in cands[0].new_text


def test_search_applies_fix_and_restores_rejected_candidates():
    files = {"f.py": SRC}

    def evaluate(c):
        ns = {}
        exec(files["f.py"], ns)
        return {"accepted": ns["shipping"](50) == 0 and ns["shipping"](10) == 5}

    res = repair.search(files.__getitem__, files.__setitem__, ["f.py"], {"shipping"}, 1, evaluate,
                        lambda: None, max_evals=50)
    assert res.found and "total >= 50" in files["f.py"]

    files["f.py"] = SRC
    res = repair.search(files.__getitem__, files.__setitem__, ["f.py"], {"shipping"}, 1,
                        lambda c: {"accepted": False}, lambda: None, max_evals=50)
    assert not res.found and files["f.py"] == SRC  # original restored


def test_workspace_round_trip_preserves_crlf(tmp_path):
    """Regression (Windows CI): restoring 'the original' must be byte-exact, whatever the line endings."""
    (tmp_path / "src").mkdir()
    raw = b"def f(x):\r\n    return x > 1\r\n"
    (tmp_path / "src" / "a.py").write_bytes(raw)
    ws = Workspace(tmp_path, writable=["src/*.py"])
    ws.write_text("src/a.py", ws.read_text("src/a.py"))
    assert (tmp_path / "src" / "a.py").read_bytes() == raw
