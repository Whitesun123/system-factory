"""Agent isolation primitives.

- run_command: subprocess with timeout, cancellation polling and a scrubbed (allow-listed) environment.
- Workspace: file access confined to one worktree, with per-role write allow-lists.
- Git: git with the user's global/system config disabled and an agent identity.

This is process-level isolation on the host, not a container. See DECISIONS.md (D5).
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

# Environment variables an agent subprocess may inherit. Everything else (API keys, tokens,
# cloud credentials, the user's git config) is dropped.
SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "TZ",
}
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|SESSION|COOKIE", re.I)

AGENT_NAME = "System Factory Agent"
AGENT_EMAIL = "agent@systemfactory.local"

# Serialises git operations that write to the shared object store / refs of a repository.
GIT_LOCK = threading.Lock()


class Cancelled(Exception):
    """Raised when a workflow's cancellation flag is observed."""


class StepTimeout(Exception):
    """Raised when a step exceeds its wall-clock deadline."""


def scrubbed_env(extra: dict | None = None, gitconfig: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k.upper() in SAFE_ENV_KEYS and not SECRET_NAME.search(k)}
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",   # mutated sources must never hit stale .pyc files
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    if gitconfig is not None:
        env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        env["HOME"] = str(gitconfig.parent)
    if extra:
        env.update(extra)
    return env


@dataclass
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int
    output: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self, tail: int = 4000) -> dict:
        d = asdict(self)
        d["output"] = self.output[-tail:]
        d["command"] = " ".join(self.argv)
        return d


def _kill_tree(p: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        p.kill()


def run_command(argv: list[str], cwd: Path | str, *, timeout: float, env: dict,
                should_stop: Callable[[], None] | None = None, poll: float = 0.1) -> CommandResult:
    """Run a command, killing it on timeout or when `should_stop` raises (cancellation / deadline)."""
    start = time.monotonic()
    p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace")
    while True:
        try:
            out, _ = p.communicate(timeout=poll)
            return CommandResult(argv, str(cwd), p.returncode, out or "", time.monotonic() - start)
        except subprocess.TimeoutExpired:
            if should_stop is not None:
                try:
                    should_stop()
                except BaseException:
                    _kill_tree(p)
                    p.communicate()
                    raise
            if time.monotonic() - start > timeout:
                _kill_tree(p)
                out, _ = p.communicate()
                return CommandResult(argv, str(cwd), -1, (out or "") + f"\n[killed after {timeout}s timeout]",
                                     time.monotonic() - start, timed_out=True)


class Workspace:
    """File access confined to `root`. Writes are only allowed to paths matching `writable` globs."""

    def __init__(self, root: Path | str, writable: list[str] | None = None):
        self.root = Path(root).resolve()
        self.writable = writable or []

    def rel(self, path: str | Path) -> str:
        p = (self.root / path).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError(f"path escapes workspace: {path}")
        return p.relative_to(self.root).as_posix()

    def read_text(self, path: str) -> str:
        rel = self.rel(path)
        if rel == ".git" or rel.startswith(".git/"):
            raise PermissionError("access to .git is not allowed")
        return (self.root / rel).read_text(encoding="utf-8")

    def write_text(self, path: str, text: str) -> None:
        rel = self.rel(path)
        if rel == ".git" or rel.startswith(".git/"):
            raise PermissionError("writes to .git are not allowed")
        if not any(fnmatch.fnmatchcase(rel, pat) for pat in self.writable):
            raise PermissionError(f"write to {rel} not permitted for this role (allowed: {self.writable})")
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


class GitError(Exception):
    pass


class Git:
    def __init__(self, gitconfig: Path, timeout: float = 60):
        self.env = scrubbed_env(gitconfig=gitconfig)
        self.timeout = timeout

    def run(self, cwd: Path | str, *args: str, check: bool = True) -> CommandResult:
        argv = ["git", "-c", "core.autocrlf=false", "-c", f"user.name={AGENT_NAME}",
                "-c", f"user.email={AGENT_EMAIL}", "-c", "commit.gpgsign=false", *args]
        res = run_command(argv, cwd, timeout=self.timeout, env=self.env)
        if check and not res.ok:
            raise GitError(f"git {' '.join(args)} failed ({res.exit_code}): {res.output.strip()[-800:]}")
        return res

    def out(self, cwd: Path | str, *args: str) -> str:
        return self.run(cwd, *args).output.strip()
