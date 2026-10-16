"""Execution backends: where target-repository and model-generated code actually runs.

Every untrusted execution in System Factory (the target's tests, the reproduction test, repair candidates,
reviewer probes and checks) goes through `StepContext.run_python`, which delegates to one backend:

    LocalExecutionBackend   isolation="process"    host Python, scrubbed environment, worktree cwd.
                                                    Fast and portable; NOT a security boundary.
    DockerExecutionBackend  isolation="container"  short-lived container per command: no network, read-only
                                                    root filesystem, worktree mounted read-only, tmpfs /tmp,
                                                    all capabilities dropped, no-new-privileges, non-root user,
                                                    PID/memory/CPU limits, forced cleanup on timeout/cancel.

Selection is per workflow (`options.execution`). A workflow with `require_isolation` never runs on the local
backend: if Docker is unavailable it fails closed (see `resolve_backend`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Protocol

from .sandbox import CommandResult, run_command, scrubbed_env

SANDBOX_IMAGE = os.environ.get("SF_SANDBOX_IMAGE", "systemfactory-sandbox:1")


class IsolationUnavailable(Exception):
    """The workflow requires container isolation and no container backend is usable."""


class ExecutionBackend(Protocol):
    name: str
    isolation: str  # "process" | "container"

    def available(self) -> tuple[bool, str]: ...

    def run_python(self, args: list[str], *, workdir: Path, ro_dirs: dict[Path, str], timeout: float,
                   should_stop: Callable[[], None] | None) -> CommandResult: ...


class LocalExecutionBackend:
    name, isolation = "local", "process"

    def available(self) -> tuple[bool, str]:
        return True, "host interpreter (process-level isolation only)"

    def run_python(self, args: list[str], *, workdir: Path, ro_dirs: dict[Path, str], timeout: float,
                   should_stop: Callable[[], None] | None) -> CommandResult:
        env = scrubbed_env(extra={"PYTHONPATH": str(workdir)})
        return run_command([sys.executable, *args], workdir, timeout=timeout, env=env, should_stop=should_stop)


_DOCKER_STATUS: dict[str, tuple[float, tuple[bool, str]]] = {}
_DOCKER_LOCK = threading.Lock()


class DockerExecutionBackend:
    """One `docker run --rm` per command. The container sees only the explicitly mounted directories."""

    name, isolation = "docker", "container"

    def __init__(self, image: str = SANDBOX_IMAGE, docker: str | None = None, cpus: str = "1.0",
                 memory: str = "512m", pids: int = 128, tmpfs_size: str = "64m", startup_slack: float = 15.0):
        self.image, self.docker = image, docker or os.environ.get("SF_DOCKER", "docker")
        self.cpus, self.memory, self.pids, self.tmpfs_size = cpus, memory, pids, tmpfs_size
        self.startup_slack = startup_slack

    def _cli_env(self) -> dict:
        # The docker CLI is a trusted host tool; untrusted code only ever sees the -e variables below.
        return dict(os.environ)

    def available(self) -> tuple[bool, str]:
        key = f"{self.docker}|{self.image}"
        with _DOCKER_LOCK:
            hit = _DOCKER_STATUS.get(key)
            if hit and time.time() - hit[0] < 30:
                return hit[1]
        status = self._probe()
        with _DOCKER_LOCK:
            _DOCKER_STATUS[key] = (time.time(), status)
        return status

    def _probe(self) -> tuple[bool, str]:
        if shutil.which(self.docker) is None:
            return False, f"docker CLI '{self.docker}' not found"
        try:
            info = subprocess.run([self.docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                                  text=True, timeout=15, env=self._cli_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"docker daemon not reachable: {e}"
        if info.returncode != 0:
            return False, "docker daemon not reachable: " + (info.stderr.strip().splitlines() or ["?"])[-1][:200]
        img = subprocess.run([self.docker, "image", "inspect", "--format", "{{.Id}}", self.image], capture_output=True,
                             text=True, timeout=15, env=self._cli_env())
        if img.returncode != 0:
            return False, f"sandbox image '{self.image}' not built (run scripts/build_sandbox_image)"
        return True, f"docker {info.stdout.strip()}, image {self.image} ({img.stdout.strip()[:19]})"

    def argv(self, args: list[str], *, workdir: Path, ro_dirs: dict[Path, str], name: str) -> list[str]:
        mounts = {Path(workdir).resolve(): "/work", **{Path(k).resolve(): v for k, v in ro_dirs.items()}}

        def translate(a: str) -> str:
            for host, inner in mounts.items():
                for form in (str(host), host.as_posix()):
                    rest = a[len(form):]
                    if a.startswith(form) and (not rest or rest[0] in "/\\"):  # whole path components only
                        return inner + rest.replace("\\", "/")
            return a

        cmd = [self.docker, "run", "--rm", "--name", name,
               "--network", "none",                       # no network at all during untrusted execution
               "--read-only",                             # immutable root filesystem
               "--tmpfs", f"/tmp:rw,nosuid,nodev,size={self.tmpfs_size}",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", str(self.pids), "--memory", self.memory, "--memory-swap", self.memory,
               "--cpus", self.cpus, "--user", "65534:65534", "--ipc", "private",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/work", "-e", "HOME=/tmp",
               "-e", "PYTHONIOENCODING=utf-8"]
        for host, inner in mounts.items():
            cmd += ["--mount", f"type=bind,source={host},target={inner},readonly"]
        return cmd + ["-w", "/work", self.image, "python", *[translate(a) for a in args]]

    def run_python(self, args: list[str], *, workdir: Path, ro_dirs: dict[Path, str], timeout: float,
                   should_stop: Callable[[], None] | None) -> CommandResult:
        name = f"sf-exec-{uuid.uuid4().hex[:12]}"
        cmd = self.argv(args, workdir=workdir, ro_dirs=ro_dirs, name=name)
        try:
            res = run_command(cmd, workdir, timeout=timeout + self.startup_slack, env=self._cli_env(),
                              should_stop=should_stop)
        finally:
            # Killing the CLI does not stop the container; always force-remove it by name. A slow removal must not
            # replace the command's own result (or exception) with a TimeoutExpired.
            try:
                subprocess.run([self.docker, "rm", "-f", name], capture_output=True, timeout=30, env=self._cli_env())
            except (OSError, subprocess.TimeoutExpired):
                pass
        if res.exit_code == 137 and not res.timed_out:
            res.output += "\n[container killed: exit 137 (out of memory or SIGKILL)]"
        return res


def make_backend(name: str) -> ExecutionBackend:
    if name == "local":
        return LocalExecutionBackend()
    if name == "docker":
        return DockerExecutionBackend()
    raise ValueError(f"unknown execution backend {name!r} (expected local or docker)")


def resolve_backend(name: str, require_isolation: bool) -> ExecutionBackend:
    """Return the configured backend, or fail closed. Never falls back from container to process isolation."""
    backend = make_backend(name)
    if require_isolation and backend.isolation != "container":
        raise IsolationUnavailable(f"this workflow requires container isolation, but execution backend is "
                                   f"'{name}' (process-level only)")
    ok, why = backend.available()
    if not ok:
        if require_isolation:
            raise IsolationUnavailable(f"container isolation required but unavailable: {why}")
        raise IsolationUnavailable(f"execution backend '{name}' unavailable: {why}")
    return backend
