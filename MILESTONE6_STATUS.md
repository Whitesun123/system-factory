# Milestone 6 status

Progress log for Milestone 6. Each sub-milestone records what was implemented, what was verified (with the actual
command or CI run), and what remains open. Nothing here is a plan presented as a result.

## Baseline (start of Milestone 6)

- `main` = `origin/main` = `838bf31`, CI green on Windows and Ubuntu. Offline suite locally: 71 passed, 1 skipped.
- Environment: Windows 11, RTX 5080 16 GB, Docker Desktop 29.6.1 (engine started for this milestone), Ollama 0.30.8
  with `qwen2.5-coder:14b`, `phi4:14b`, `mistral-nemo:12b`, `gemma3:12b`, `qwen3:14b` on D:.

## Gap analysis

| # | Gap | Impact | Sub-milestone |
|---|---|---|---|
| G1 | Untrusted code (target tests, model-written patches and checks) runs on the host with process-level isolation only | Main architectural security limitation | M6.1 |
| G2 | Committed evidence manifests reference a pre-rewrite project SHA and `git_dirty: true` (recorded at export time from a dirty tree) | Evidence integrity | M6.2 |
| G3 | A failing reviewer check is treated as a confirmed defect even when its expected value is not grounded in any requirement; conflicting expectations are not detected; approvals without inspection count as reviews | False blocks / unjustified confirmations | M6.2 |
| G4 | Reviewer reliability measured on 5 cases only | Model selection not evidence-based | M6.3 |
| G5 | Final executed-checks design never validated end to end with real models; scenarios B–E unrun | Core claim unverified | M6.4 |
| G6 | No coverage measurement, lint, type checks or dependency locking; no separated real-model CI workflow | Engineering quality | M6.5 |
| G7 | Dashboard shows iterations as a list; no side-by-side diff, budget view or evidence-validity states | Reviewability | M6.6 |
| G8 | No independent review of the changes | Self-assessment bias | M6.7 |

## M6.1: Execution backends and Docker sandbox (implemented, verified locally)

- `systemfactory/execution.py`: `ExecutionBackend` protocol, `LocalExecutionBackend` (process isolation),
  `DockerExecutionBackend` (container isolation), `resolve_backend` (fail closed). `StepContext.run_python`, the single
  choke point for every untrusted execution, delegates to the workflow's backend. There is no second pipeline.
- Container controls: `--network none`, `--read-only`, worktree and probe directory mounted read-only, tmpfs `/tmp`,
  `--cap-drop ALL`, `no-new-privileges`, user 65534, PID/memory/CPU limits, no Docker socket, no host environment,
  unique name plus forced `docker rm -f` cleanup. Details and trust policy: `sandbox/README.md`.
- Sandbox image: `sandbox/Dockerfile` with the base pinned by digest and test dependencies pinned with
  `--require-hashes`. Built by `scripts/build_sandbox_image.{ps1,sh}`.
- Policy: the real-LLM runtime defaults to `execution=docker, require_isolation=True`. A workflow that requires isolation is
  **blocked** with nothing executed if Docker or the image is unavailable. Each `test_runs` row records its backend.
- Verified locally (Docker Desktop): `tests/test_docker_sandbox.py`, 12 passed. Covered: host files and secrets invisible,
  workspace/probes/root filesystem read-only, no network (only `lo`), uid 65534 with zero capabilities and NoNewPrivs,
  no Docker socket, PID limit (process explosion stopped), memory limit (exit 137), timeout cleanup, cancellation cleanup,
  a full deterministic workflow inside containers, and fail-closed behavior. Without Docker: 3 passed, 9 skipped with
  reason. Full offline suite: **83 passed, 1 skipped**.
- Cost measured: a deterministic shipping workflow takes 14.6 s on Docker; each container command takes about 1 s.
- Open: Windows CI runners have no Linux Docker daemon, so container tests run only in the Ubuntu job.

## M6.2: Evidence integrity and hardened review evidence (implemented, verified locally)

- **Root cause of the stale manifests:** `evidence.collect()` stamped the System Factory revision and dirty flag at
  *export* time. The committed examples were exported while the project was at `e9c444c` with uncommitted 5.1 changes,
  so `git_dirty: true` was accurate, and the SHA became unreachable after the history cleanup.
- **Fix:** provenance (`source_revision`, `source_dirty`, modified files) is recorded at submission. Manifests separate
  `system_factory.run` from `system_factory.export` and label the demo repository's SHAs as run-local
  (`target_repository`). The demo base commit is deterministic (fixed dates), so any machine produces the same base SHA.
- **Regenerated evidence:** the three packages in `examples/evidence/` were produced and exported from a fresh clone of
  `1cb0148` (run and export both clean). The first attempt recorded `export.source_dirty: true` because I had deleted
  tracked files in the clone before exporting; that was true, so it was re-exported correctly instead of being edited.
- **Release check:** `tests/test_evidence_integrity.py` fails when a package's run revision is missing, not in the
  history of HEAD, or dirty, or when a package is incomplete or unsanitized. It failed on the old examples, as intended.
- **Evidence states:** confirmed (grounded in a requirement, or corroborated by another reviewer), unsupported,
  conflicting, unverified, invalid, passed. Unsupported or conflicting evidence, and approvals without inspection, give an
  explicit `unresolved` decision that escalates. Value grounding needs an exact quoted string or a whole sentence; a
  regression test covers substring and number coincidences, a weakness found while testing.
- **Gate additions:** the reviewed commit must be the final HEAD with a clean worktree; enough reviews must have
  inspected the change; there must be no unresolved evidence.
- Tests: `tests/test_evidence_states.py` (15) and `tests/test_evidence_integrity.py` (7).
