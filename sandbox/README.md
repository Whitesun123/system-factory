# Execution sandbox (Docker backend)

`DockerExecutionBackend` (`systemfactory/execution.py`) runs every untrusted command — the target repository's
tests, the reproduction test, repair candidates, reviewer probes and checks — in a fresh container:

| Control | Setting |
|---|---|
| Network | `--network none` |
| Root filesystem | `--read-only` |
| Workspace | git worktree bind-mounted **read-only** at `/work`; the coder edits files from the host side only |
| Probes | the workflow's probe directory mounted read-only at `/sf/probes` |
| Writable space | `--tmpfs /tmp` (64 MB, `nosuid,nodev`) |
| Privileges | `--cap-drop ALL`, `--security-opt no-new-privileges`, user `65534:65534`, never `--privileged` |
| Resources | `--pids-limit 128`, `--memory 512m` (no swap), `--cpus 1.0` |
| Environment | only `PYTHONDONTWRITEBYTECODE`, `PYTHONPATH`, `HOME`, `PYTHONIOENCODING`; no host variables, credentials or git config |
| Docker socket / host dirs | not mounted |
| Lifetime | one `docker run --rm` per command, unique name, `docker rm -f` in a `finally` block (timeout and cancel included) |

## Trust policy

- **Image build (trusted, networked):** `scripts/build_sandbox_image.ps1` / `.sh` builds the image from this directory.
  The base image is pinned by digest. Python packages are installed with `--require-hashes` from
  `requirements.txt`. This is the only phase with network access. No file from a target repository is used.
- **Untrusted execution (no network):** target code and model-written code run only inside the container described
  above. Target repositories cannot install dependencies. A target that needs packages beyond the image is out of
  scope until a separate, reviewed dependency-provisioning step exists.
- **Fail closed:** a workflow with `require_isolation` (the default for the real-LLM runtime) is **blocked** if the
  daemon or the image is unavailable. It never falls back to the local backend.

## Limitations

- Containers share the host kernel; this is not a VM. Kernel exploits are out of scope.
- On Docker Desktop for Windows the containers run in the WSL2 VM; the read-only bind mount is enforced by Docker.
- The git operations (worktree, commit, diff) run on the host with a hermetic git config; they process the target's
  files but do not execute them.
