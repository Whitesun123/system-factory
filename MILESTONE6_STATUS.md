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

## M6.3: Reviewer evaluation framework (implemented, run with real inference)

- `evaluation/`: 23 labelled cases (11 defective, 12 correct) over 12 materialised code bases. Every case is a valid
  bug fix: the reproduction test fails before and passes after, and the existing tests pass. The labels are checked
  against independent oracles (`tests/test_reviewer_eval_dataset.py`) and never reach the reviewers.
- Each case is scored by the raw verdict and by the pipeline decision. Checks run in the Docker sandbox.
- A per-call checkpoint cache is keyed by case, role, model, digest and prompt hash. The budget is cumulative across
  runs.
- Run: 30.4 minutes of real inference (budget 30), Ollama 0.30.8, one provider. 22 (case, model) pairs were **not
  executed**; they are listed in `evaluation/results/REPORT.md`.
- **Measured (comparable 18 cases, after the fixes below):** raw verdicts of phi4, qwen3 and mistral-nemo caught 0 of
  9 defects (qwen3 failed on 1); gemma3 caught 2. The evidence pipeline caught 6 of 9 for phi4 alone (0 false blocks). The selected phi4 +
  qwen3 panel caught 6 of 9 with 1 false approval, 0 false blocks, 8 of 9 correct changes approved and 2 escalations.
  The deterministic baseline caught 0 of 9.
- **Pipeline defects found by the evaluation and fixed** (regression tests in `tests/test_evidence_states.py`):
  1. an expected exception was compared as a string, which blocked correct code that raised it;
  2. a mis-escaped backslash made a model's check test a different input;
  3. a check whose `function` field used the prompt's inventory format was rejected, or not counted as an
     inspection, although its call was valid. Most gemma3 and qwen3 reviews were affected, so most correct
     changes were escalated. The first real run (A1, on `ac0784f`) showed the same escalation, and it was stopped
     so that the real scenarios run on the fixed code.
  The cached reviews were re-scored after each fix with no new inference. `inference-run-1.json` keeps the
  scores from before the fixes. **The post-fix numbers are optimistic: the fixes were developed on this dataset.**
- **Selection:** Reviewer A `phi4:14b`, Reviewer B `qwen3:14b` (DECISIONS.md D23). The margins over the other
  panels are one or two cases.
- Observed failure: `qwen3:14b` returned an empty reply after exhausting its output on thinking tokens (recorded as
  a reviewer error, never as an approval). Mean latency was 43 s (phi4: 16 s).

## M6.5: Engineering quality (implemented, verified locally; CI pending)

- ruff (E, F, W, B) and mypy (`check_untyped_defs`): clean. Fixing the 75 mypy errors exposed two latent bugs, both
  fixed with regression tests: a crash on a malformed diff hunk header, and an LLM reviewer that could run
  without a call recorder.
- Coverage of the control-plane modules: 95% overall (orchestrator 96%, statemachine 100%, loop 96%, store 95%,
  context 92%, execution 90%, sandbox 90%). The whole package is at 87%. CI fails below 85% on the control plane.
- `constraints.txt` (generated from a clean virtual environment) and `requirements-dev.txt` pin the versions.
- `real-model-eval.yml` runs only via manual dispatch on `main`, on a self-hosted `gpu-ollama` runner. It uses no
  secrets, and its inputs reach the shell only as environment variables. The file alone cannot keep pull-request
  code off a self-hosted runner, so no runner is registered; the conditions for adding one are in the workflow.

## M6.7: Independent review (fresh context; same model family, not model diversity)

Findings and fixes (regression tests in `tests/test_review_findings.py`):

| # | Finding | Severity | Fix |
|---|---|---|---|
| R1 | A check killed by a timeout or OOM was `invalid`, and its review still counted as an inspection | high (false approval) | `timeout` outcome is a failure; only checks that produced a result count as an inspection |
| R2 | TypeError/NameError raised *inside the patched code* was classified as a broken probe | medium-high (false approval) | probe-error only if the innermost frame is the doctest itself |
| R3 | A "located" finding in a nonexistent or unchanged file counted as an inspection | medium | must name a changed file or a function of the changed files |
| R4 | `from <changed module> import os` passed the free-form probe vetting; `str.format` reached internals | medium (defence in depth) | only names the module defines may be imported; `format`/`format_map` forbidden; evaluation defaults to Docker |
| R5 | The evaluation harness ran without Docker, turning infrastructure failures into "caught defects" | medium (honesty) | `resolve_backend(..., require_isolation=True)`: fails closed |
| R6 | The raw baseline counted reviewer errors as approvals | medium (metrics) | `raw: "error"` |
| R7 | Grounding, and the deterministic spec reviewer, used docstrings from the *patched* files | low-medium | documentation from the reproduction checkpoint |
| R8 | Small: file attribution of qualified checks, path-prefix translation without a separator boundary, a `docker rm` timeout masking the result, provenance ignoring untracked files, the budget reset by a case subset, the self-hosted-runner claim | low | each fixed; the runner claim was corrected (no runner is registered; conditions documented) |

The review confirmed as sound: the Docker flags and fail-closed backend, no bypass of `run_python`, no label leakage,
no silent simulation in the real runtime, the gate checks, and dashboard escaping.

## M6.4: Real end-to-end validation (run twice on clean clones)

`scripts/real_demo.py --scenarios A,B,C,D,E --repeat 2`, from clean clones of `c8026e0` and of `7f6a5b2` (the final
pipeline code). Coder `qwen2.5-coder:14b`; reviewers `phi4:14b` and `qwen3:14b`; one provider; Docker sandbox.

| Scenario | Run 1 (`c8026e0`) | Run 2 (`7f6a5b2`) |
|---|---|---|
| A1/A2 real fix | pass in 1 iteration, gate SUCCESS, oracle satisfied (71 s, 70 s) | same (68 s, 79 s) |
| B1/B2 revision after review | revise then pass, gate SUCCESS, oracle satisfied (161 s, 161 s) | same (163 s, 184 s) |
| C feedback withheld | stalled: same fingerprint `ec67817129`, escalated | same |
| D1 unreachable / D2 1 s timeout / D3 cancel | failed (3 failed calls) / failed / cancelled | same |
| E approval guard | 4 approved (all gate SUCCESS), 4 refused; `main` unchanged | same |

An earlier attempt on `ac0784f` was **stopped after A1**. A1 had produced a correct patch (oracle satisfied), but it
was escalated because Reviewer B's checks were rejected over the function-name format (M6.3, defect 3). Those
packages are not published. These are repeated observations at temperature 0 with fixed seeds on two synthetic
bugs; they show reproducibility under fixed sampling, not an automation rate.

## M6.6: Dashboard and documentation

- Loop flow from recorded step and iteration state, budget bars, an iteration comparison (finding, required probe,
  result, side-by-side patches), evidence-state chips, an execution-backend badge, ARIA tabs and progress bars, and
  a responsive layout. Checked at 1440, 1000 and 768 px. A media-query ordering bug that broke the single-column
  tablet layout was fixed.
- Screenshots `docs/screenshots/04`-`08` were captured with headless Edge (fresh profile) from the recorded run-2
  workflows. No paths are shown (the worktree displays as `<data>/...`).
- README rewritten (problem, loop, control plane vs roles, real vs simulated, sandbox, measured evaluation, real
  scenarios, case study, limitations, roadmap); DEMO, PRESENTATION and ARCHITECTURE updated; D21-D23 added.
- **License:** MIT, chosen by the owner after Milestone 6 (`LICENSE`).

## Release audit (October 16, 2026)

Focused review of the control plane, plus a fresh-clone smoke test (Windows `setup.ps1`, a manual venv, and the
Linux quick start on Ubuntu 24 under WSL). Findings:

| Severity | Finding | Status |
|---|---|---|
| High (demo) | The dashboard's runtime selector defaulted to "Real local inference (Ollama)" even with no Ollama, so following "pick a sample and press Submit" started a real-runtime workflow that could only fail | fixed: starts on the server's default runtime (`SF_RUNTIME`, simulated); `tests/test_release_audit.py` |
| Medium | A relative `SF_DATA_DIR` (e.g. `../data`) made every bug workflow fail at `workspace` (git and sandboxed commands run with other working directories) | fixed: `Settings` resolves `data_dir` to an absolute path; regression test |
| Medium | A model reply that was valid JSON of the wrong shape (a list, `edits` as a string, a numeric `path`) raised inside the coding step and failed the workflow permanently (fail-closed, but one malformed reply ended the run) | fixed: `parse_json` requires an object; the coder validates action shapes and returns errors to the model; regression test |
| Low | `str.lstrip("./")` on model-supplied paths also strips leading dots (`.github/x` → `github/x`); such paths are refused rather than escaping | documented |
| Low | On its last turn the coder's submitted patch is kept even if its self-check failed; the deterministic `testing` step then fails it | by design, documented |
| Low | Evidence packages record the backend per test run, not per reviewer check (checks use the same execution path) | documented in README limitations |

Confirmed sound in this audit: approval requires `awaiting_approval` and gate SUCCESS inside one transition; the real
runtime blocks without Docker and never substitutes simulated reviewers; workspace paths are resolved (symlink
escapes refused, `.git` blocked, writes glob-restricted); foreign `Host` and cross-origin state-changing requests get
403; evidence export works from a fresh clone; the demo repository's `main` is unchanged after approval.

Scenario B was re-verified claim by claim against its saved records: [docs/SCENARIO_B_VERIFICATION.md](docs/SCENARIO_B_VERIFICATION.md).
The evaluation figures in the README were re-computed from `evaluation/results/report.json` and match.
