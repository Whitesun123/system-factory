# Milestone 5 status: local LLM integration, evidence logging, provider infrastructure

Date: 2026-10-15. This document separates **(1) runtime results observed earlier in this milestone** from
**(2) checks executed for this release**. Nothing here claims the models or reviewers are production-ready.

## Completed implementation

| Component | Where | State |
|---|---|---|
| Ollama provider: configurable URL, structured JSON output, typed errors (unavailable / model missing / timeout / invalid output), model digests from `/api/tags` | `systemfactory/llm.py` | implemented |
| Per-call evidence: every model call, including failed ones, recorded in `llm_calls` (model, digest, execution `real-local`/`simulated`, status, timings, tokens reported by Ollama, sanitized prompt/response, hashes) | `llm.Recorder`, `store.py` | implemented |
| Real coding agent: allow-listed JSON actions (`list_files`, `read_file`, `run_tests`, `submit` search/replace edits); edits limited to plan target files; syntax-checked; rejected-patch fingerprints refused; post-submit tests; feedback, required probes and rejected diffs in the prompt; per-attempt seed; every action recorded in `tool_calls` | `systemfactory/coders.py` | implemented |
| Real reviewers A (requirements/correctness/architecture) and B (edge cases/regressions/security/maintainability), each receiving its own copy of the bundle | `reviewers.OllamaReviewer` | implemented |
| Executed specification checks: reviewers must propose `{function, call, expected}` checks; calls are validated (literal or pure-builtin arguments only) and executed in the sandbox; only executed failures block | `reviewers.check_to_probe`, `loop.aggregate` | implemented |
| Evidence rules: unconfirmed or probe-less LLM blockers are downgraded; checks confirmed by execution are upgraded; malformed probes (`NameError`, `ImportError`, …) never count as confirmation | `loop.classify_probe`, `loop.aggregate` | implemented |
| Honest diversity labels: several models on Ollama are reported as **one provider** | `reviewers.diversity` | implemented |
| Real runtime never silently substitutes simulated reviewers | `reviewers.resolve(allow_simulated=False)` | implemented |
| Test-run logging: every pytest command (commit, exit code, pass/fail counts, duration, output tail) in `test_runs` | `context.run_pytest` | implemented |
| DB migrations: new tables `llm_calls`, `tool_calls`, `test_runs`; new `reviews` columns; old databases upgrade in place | `store.py` | implemented |
| Evidence export: `summary.md`, `run-manifest.json`, `iterations.json`, `review-findings.json`, `test-results.json`, `quality-gate.json`, `patch.diff`, plus `llm-calls.json`, `tool-calls.json`, `events.json`; sanitized | `systemfactory/evidence.py`, CLI `export`, `GET /api/workflows/{id}/export` | implemented |
| API: provider/model status in `/api/health` (cached 10 s), `/api/reviewers`, `/api/llm_calls/{id}`, runtime selection on submit | `systemfactory/api.py` | implemented |
| Dashboard: runtime and provider panel, runtime selector, feedback-ablation switch, REAL/SIMULATED badges, executed checks per reviewer, **agents** tab (model calls, tool calls, test commands), evidence export button | `systemfactory/static/index.html` | implemented, **not viewed in a browser** after the last change |
| New demo bug `checkout` (two related defects; the report mentions one) | `demo/shopcart_template/shopcart/checkout.py` | implemented |
| Scripts: `setup.ps1`, `start.ps1`, `test.ps1`, `pull_models.ps1`, `check_models.py` (no inference), `real_demo.py`, `eval_reviewers.py` | `scripts/` | written; `real_demo.py` **not executed** |
| CI: offline suite on Ubuntu and Windows | `.github/workflows/ci.yml` | written, not yet run on GitHub |

## Checks executed for this release (offline, no LLM)

- `python -m compileall` over `systemfactory/`, `scripts/`, `tests/`: OK.
- Import of every module (`api`, `cli`, `coders`, `evidence`, `llm`, `reviewers`): OK.
- `python -m systemfactory --help`: OK.
- Migration check: a Milestone 2 database gains all new tables and columns, and its workflows remain readable: OK.
- Tests, within the 2-minute budget: **36 passed, 0 failed** (44 s). Selected: `tests/test_llm_offline.py` (9 tests,
  including the real Ollama client against an unreachable port, and a simulated end-to-end evidence export),
  `tests/test_statemachine_store.py`, `tests/test_sandbox_repair.py`, and the pure-logic and adapter tests of
  `tests/test_review_loop.py`.
- **Not re-run in this release:** the longer simulated end-to-end tests (`tests/test_workflows.py`, `tests/test_api.py`
  and the workflow tests in `tests/test_review_loop.py`), about 3–4 minutes. The full suite (46 tests at the time) passed
  at the start of Milestone 5, before these changes. Run `.\scripts\test.ps1` to verify.
- No test in the suite needs a running LLM, so none were skipped for that reason.
- Secret scan of the staged files and of the git history: see the commit section below.

## Real LLM experiments performed earlier in this milestone (observed, not re-run)

Environment: Windows 11, RTX 5080 16 GB, Ryzen 7 7800X3D, 32 GB RAM, Ollama 0.30.8, models stored on D:.

Installed models (from Ollama's model list, digest prefix):

| model | digest | size | role |
|---|---|---|---|
| `qwen2.5-coder:14b` | `9ec8897f747e` | 9.0 GB Q4_K_M | default coder |
| `phi4:14b` | `ac896e5b8b34` | 9.1 GB Q4_K_M | default Reviewer A |
| `mistral-nemo:12b` | `e7e06d107c6c` | 7.1 GB Q4_0 | default Reviewer B |
| `gemma3:12b` | (installed for benchmarking) | | candidate |
| `qwen3:14b` | (installed for benchmarking) | | candidate |

Observations:

1. **Smoke test:** all three default models answered with valid schema-constrained JSON on the GPU (about 13 GB VRAM at
   12k context). Throughput: qwen2.5-coder 38, phi4 73, mistral-nemo 90 tokens/s. Cold loads took 7–23 s.
   mistral-nemo answered a simple reasoning question wrongly (3 instead of 4).
2. **Real end-to-end, bulk-discount bug (scenario A):** the real coder called `run_tests`, then submitted one edit
   fixing both boundaries. Both real reviewers approved, and the quality gate passed in 43 s total. The fix matched the
   specification.
3. **Real end-to-end, checkout bug (scenario B attempt):** the coder fixed only the reported function. Both real
   reviewers **approved an incomplete fix** (using the earlier review prompt). That run was a genuine false approval,
   and it led to the executed-checks design.
4. **Reviewer benchmark** (`scripts/eval_reviewers.py`: 5 labeled patches, 2 incomplete and 3 correct, scored on the
   effective outcome after evidence rules), final version with executed checks:

   | model | Reviewer A role | Reviewer B role |
   |---|---|---|
   | phi4:14b | 5/5 (0 false approvals, 0 false blocks) | 5/5 |
   | qwen3:14b | 3/5 (before a validator fix) | 4/5 |
   | gemma3:12b | 3/5 (before a validator fix) | 3/5 |
   | mistral-nemo:12b | 3/5 | 3/5 |

   In earlier runs, before the executed checks, phi4 raised two false blockers on a correct patch. The probe
   re-execution downgraded both. Five cases is a very small sample, and these numbers are indicative only.

## Known problems

- **Reviewer B default** (`mistral-nemo:12b`) scored 3/5 in the benchmark and wrote very short reviews. phi4 scored better
  in both roles, but using it for both would put A and B on the same model. The defaults were left unchanged for this
  release; set `SF_REVIEWER_B_MODEL` to choose.
- The executed-checks mechanism depends on the reviewer deriving the correct expected value. A wrong expectation causes
  a false block, which surfaces as a stall or escalation rather than a bad merge.
- The real coder and reviewers have not been run end to end since the executed-checks and validator changes.
- Process-level isolation only (no container). Example code from issues and reviewer checks is executed on the host
  with a scrubbed environment and literal-argument validation.
- The SQLite DB stores sanitized prompts and responses locally. It is git-ignored and never pushed.

## Features requiring additional validation

- Real scenarios B–E with the final code: `python scripts\real_demo.py`.
- Dashboard **agents** tab and export button in a browser.
- CI on GitHub (Ubuntu path differences, process-tree kill on Linux).
- Claude and command reviewer adapters against real providers (never run; paid use needs approval).

## Recommended next steps

1. Run `.\scripts\test.ps1` (full offline suite) and `python scripts\real_demo.py` with Ollama running. Read the
   generated evidence in `data-real-demo/evidence/`.
2. Decide the Reviewer B model from a larger labeled benchmark (more than 5 cases, including correct patches).
3. Containerize agent command execution (Docker), then consider making the repository public.

## Milestone 5.1 (review fixes, offline only)

- CI fix: `tests/test_api.py` asserted an obsolete field (`claude_runtime_available`); it now checks the current
  `runtime_info()` contract. CI matrix no longer fails fast, so Windows and Ubuntu both report.
- Tests are hermetic: `tests/conftest.py` clears `SF_*`/`ANTHROPIC_*` and points Ollama at a closed port.
- Security fixes with regression tests (`tests/test_security_regressions.py`):
  F1 doctest-line injection via multi-line `expected` in reviewer checks; F2 untrusted (model-written) probes are
  statically vetted before execution; F3 localhost API now enforces a Host allow-list (DNS rebinding) and refuses
  cross-origin state changes (CSRF); F4 approval decision is gated by the atomic state transition (no duplicate
  approval rows under concurrency); F5 coder `read_file` errors become tool errors; F6 POSIX timeouts kill the
  whole process group.
- Dashboard shows worktree paths relative to the data dir (no user paths in screenshots); deep links `#wf=N&tab=…`.
- Full offline suite locally (Windows): **69 passed, 1 skipped** (F6 is POSIX-only).
- Deterministic cases verified by tests: A `test_first_implementation_fails_review_then_is_corrected`,
  B `test_repeated_solution_is_detected_and_escalated`, C `test_quality_gate_rejects_test_tampering_and_dangerous_code`
  + `test_case_c_llm_coder_cannot_edit_tests_or_escape_scope` + `test_approval_requires_quality_gate_success`.
- Sanitized deterministic evidence: `examples/evidence/case-a-revision-after-review/`,
  `examples/evidence/case-b-repeated-solution-escalated/`. Real screenshots: `docs/screenshots/`.
- Windows CI root cause (found from the CI log, reproduced locally with an `autocrlf=true` clone): Windows checkouts
  turned the demo template into CRLF, and `Workspace.read_text` used universal newlines, so restoring a rejected candidate
  rewrote the file as LF and left the worktree dirty (14 workflow tests failed on Windows only). Fixed with byte-exact
  reads, LF demo generation and `.gitattributes`; regression tests `test_workspace_round_trip_preserves_crlf` and
  `test_workflow_succeeds_from_a_crlf_checkout`.
- `PRESENTATION.md` and `DEMO.md` now describe both runtimes and list the real-model scenarios that remain unverified;
  `DECISIONS.md` gained D18–D20 for the Milestone 5 decisions.
- Still true: isolation is process-level, not a container, and is not suitable for untrusted repositories. Real-model
  scenarios B–E have not been run with the final code.
