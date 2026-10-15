"""Adversarial tests of the Docker execution backend, using harmless synthetic payloads.

Skipped when no Docker daemon / sandbox image is available (e.g. GitHub's Windows runners). The fail-closed tests at
the bottom run everywhere and need no Docker.
"""
import os
import subprocess
import sys
import time

import pytest

from systemfactory.execution import DockerExecutionBackend, IsolationUnavailable, resolve_backend
from systemfactory.sandbox import Cancelled
from systemfactory.statemachine import WF

from .conftest import run_all, submit_sample

BACKEND = DockerExecutionBackend()
DOCKER_OK, DOCKER_WHY = BACKEND.available()
needs_docker = pytest.mark.skipif(not DOCKER_OK, reason=f"docker sandbox unavailable: {DOCKER_WHY}")


@pytest.fixture
def box(tmp_path):
    work = tmp_path / "work"
    probes = tmp_path / "probes"
    work.mkdir()
    probes.mkdir()
    (work / "inside.txt").write_text("workspace file", encoding="utf-8")
    (probes / "probe.txt").write_text("probe file", encoding="utf-8")
    (tmp_path / "host-secret.txt").write_text("TOP-SECRET-HOST-VALUE", encoding="utf-8")

    def run(code, timeout=30, should_stop=None):
        return BACKEND.run_python(["-c", code], workdir=work, ro_dirs={probes: "/sf/probes"}, timeout=timeout,
                                  should_stop=should_stop)
    return run, work, probes, tmp_path


def _no_leftover_containers():
    out = subprocess.run(["docker", "ps", "-a", "--filter", "name=sf-exec-", "--format", "{{.Names}}"],
                         capture_output=True, text=True).stdout.strip()
    return out == ""


@needs_docker
def test_host_files_and_secrets_are_not_visible(box, monkeypatch):
    run, work, _, tmp = box
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-host-key-value")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-host-token-value")
    code = ("import os, glob\n"
            "print('KEY', os.environ.get('ANTHROPIC_API_KEY'), os.environ.get('GITHUB_TOKEN'))\n"
            "print('ENV', sorted(os.environ))\n"
            "print('ROOT', sorted(os.listdir('/')))\n"
            "hits = [p for d in ('/work', '/sf', '/tmp', '/home', '/root', '/mnt', '/media', '/opt', '/srv')\n"
            "        for p in glob.glob(d + '/**/*secret*', recursive=True)]\n"
            "print('HITS', hits, glob.glob('/work/../*secret*'))\n"
            "print('INSIDE', open('/work/inside.txt').read())\n")
    res = run(code)
    assert res.ok, res.output
    assert "KEY None None" in res.output and "synthetic-host-key-value" not in res.output and "synthetic-host-token-value" not in res.output
    env = eval(res.output.split("ENV ")[1].splitlines()[0])
    # only our explicit -e variables plus the base image's own ENV (GPG_KEY is the public python release key id)
    assert set(env) <= {"PATH", "HOSTNAME", "HOME", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
                        "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PIP_NO_INDEX", "PYTHONPATH",
                        "PYTHONIOENCODING"}, env
    assert "HITS [] []" in res.output and "TOP-SECRET-HOST-VALUE" not in res.output
    assert "INSIDE workspace file" in res.output


@needs_docker
def test_workspace_and_root_filesystem_are_read_only(box):
    run, work, probes, _ = box
    code = ("import os\n"
            "for p in ['/work/inside.txt', '/work/new.txt', '/sf/probes/probe.txt', '/usr/local/evil.txt', '/etc/x']:\n"
            "    try:\n"
            "        open(p, 'w').write('tampered'); print('WROTE', p)\n"
            "    except OSError as e:\n"
            "        print('DENIED', p, type(e).__name__)\n"
            "open('/tmp/scratch.txt', 'w').write('ok'); print('TMP OK')\n")
    res = run(code)
    assert res.ok, res.output
    assert "WROTE" not in res.output and res.output.count("DENIED") == 5 and "TMP OK" in res.output
    assert (work / "inside.txt").read_text(encoding="utf-8") == "workspace file"
    assert not (work / "new.txt").exists()
    assert (probes / "probe.txt").read_text(encoding="utf-8") == "probe file"


@needs_docker
def test_network_is_disabled(box):
    run, *_ = box
    code = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3); print('CONNECTED')\n"
            "except OSError as e:\n"
            "    print('NO NETWORK', type(e).__name__)\n"
            "print('IFACES', sorted(n for _, n in socket.if_nameindex()))\n")
    res = run(code)
    assert "NO NETWORK" in res.output and "CONNECTED" not in res.output
    assert "IFACES ['lo']" in res.output


@needs_docker
def test_runs_unprivileged_without_capabilities_or_docker_socket(box):
    run, *_ = box
    code = ("import os\n"
            "print('UID', os.getuid(), os.getgid())\n"
            "caps = [l for l in open('/proc/self/status') if l.startswith(('CapEff', 'CapPrm', 'NoNewPrivs'))]\n"
            "print(''.join(caps))\n"
            "print('SOCK', os.path.exists('/var/run/docker.sock'))\n")
    res = run(code)
    assert res.ok, res.output
    assert "UID 65534 65534" in res.output
    assert "CapEff:\t0000000000000000" in res.output and "CapPrm:\t0000000000000000" in res.output
    assert "NoNewPrivs:\t1" in res.output and "SOCK False" in res.output


@needs_docker
def test_pid_limit_stops_process_explosion(box):
    run, *_ = box
    code = ("import subprocess, sys\n"
            "procs = []\n"
            "try:\n"
            "    for i in range(400):\n"
            "        procs.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']))\n"
            "    print('SPAWNED ALL')\n"
            "except OSError as e:\n"
            "    print('LIMITED AT', len(procs), type(e).__name__)\n"
            "for p in procs: p.kill()\n")
    res = run(code, timeout=60)
    assert "SPAWNED ALL" not in res.output and "LIMITED AT" in res.output, res.output[-500:]
    assert int(res.output.split("LIMITED AT")[1].split()[0]) < 128


@needs_docker
def test_memory_limit_kills_the_container(box):
    run, *_ = box
    res = run("blocks = [bytearray(64 * 1024 * 1024) for _ in range(20)]\nprint('ALLOCATED 1.25GB')", timeout=60)
    assert "ALLOCATED" not in res.output and res.exit_code == 137, (res.exit_code, res.output[-300:])
    assert _no_leftover_containers()


@needs_docker
def test_timeout_kills_and_removes_the_container(box):
    run, *_ = box
    BACKEND.startup_slack = 5.0
    t0 = time.time()
    res = run("import time; print('start', flush=True); time.sleep(120)", timeout=2)
    assert res.timed_out and not res.ok
    assert time.time() - t0 < 40
    assert _no_leftover_containers()


@needs_docker
def test_cancellation_stops_the_container(box):
    run, *_ = box
    t0 = time.time()

    def stop():
        if time.time() - t0 > 3:
            raise Cancelled("cancel requested")
    with pytest.raises(Cancelled):
        run("import time; time.sleep(120)", timeout=120, should_stop=stop)
    assert _no_leftover_containers()


@needs_docker
def test_full_workflow_runs_inside_the_container(orch):
    wf_id = submit_sample(orch, "shipping", options={"execution": "docker", "require_isolation": True})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.AWAITING_APPROVAL, wf["error"]
    runs = orch.store.get_test_runs(wf_id)
    assert runs and {r["backend"] for r in runs} == {"docker (container)"}
    assert _no_leftover_containers()


# ------------------------------------------------------------------ fail closed (no Docker needed)
def test_isolation_required_with_local_backend_fails_closed():
    with pytest.raises(IsolationUnavailable, match="requires container isolation"):
        resolve_backend("local", require_isolation=True)


def test_isolation_required_without_docker_blocks_and_executes_nothing(orch, monkeypatch):
    monkeypatch.setenv("SF_DOCKER", "docker-binary-that-does-not-exist")
    wf_id = submit_sample(orch, "shipping", options={"execution": "docker", "require_isolation": True})
    run_all(orch)
    wf = orch.store.get_workflow(wf_id)
    assert wf["state"] == WF.BLOCKED and "container isolation required but unavailable" in wf["error"]
    assert orch.store.get_test_runs(wf_id) == []  # nothing ran on the host instead
    assert orch.store.get_step(wf_id, "reproduce")["status"] == "blocked"


def test_real_runtime_requires_isolation_by_default(orch):
    opts = orch.settings.runtime_options("ollama")
    assert opts["execution"] == "docker" and opts["require_isolation"] is True
