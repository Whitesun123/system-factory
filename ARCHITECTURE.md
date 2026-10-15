# Architecture

## Overview

```
              ┌───────────────────────────── FastAPI (localhost) ──────────────────────────────┐
  Dashboard ──┤  /api/issues  /api/workflows/{id}  approve | reject | cancel | retry  /health  │
  CLI ────────┤                                                                                │
              └───────────────┬────────────────────────────────────────────────▲─────────────┘
                              │ submit / human actions                         │ read state
                              ▼                                                │
                     ┌─────────────────┐   claim (lease)   ┌──────────────────┴──────────────┐
                     │   Orchestrator  │◄──────────────────│ Store (SQLite, WAL)             │
                     │  deterministic  │──────────────────►│ issues, workflows, steps,       │
                     │  state machine  │  every transition │ attempts, events, evidence,     │
                     └────────┬────────┘  attempt, log,    │ approvals                       │
                              │           evidence         └─────────────────────────────────┘
               runs one step at a time, with retries, deadlines, budgets, cancellation
                              ▼
   ┌────────┬─────────────┬──────────────┬───────┬──────┬────────┬──────┬──────────┬────────┬─────────┐
   │ triage │requirements │ architecture │ infra │  qa  │ coding │  qa  │ security │ review │  human  │
   │        │   (PRD)     │    (plan)    │worktree│repro │ repair │ test │   scan   │checklist│approval │
   └────────┴─────────────┴──────────────┴───┬───┴──┬───┴───┬────┴──┬───┴──────────┴────────┴─────────┘
                                             ▼      ▼       ▼       ▼
                                  data/worktrees/wf-<id>  (branch sf/wf-<id>)  ← separate demo repo
                                  scrubbed env · write allow-list · timeouts · hermetic git
```

## Components

| Component | File | Responsibility |
|---|---|---|
| State machine | `statemachine.py` | Workflow states, the only legal transitions, pipelines per work type |
| Store | `store.py` | Persistent state, atomic claims (`BEGIN IMMEDIATE`), audit log, evidence, approvals |
| Orchestrator | `orchestrator.py` | Drives steps; attempts and retries with backoff; blocked/failed/cancelled handling; human actions |
| Worker | `orchestrator.py` | Leases queued workflows, bounded concurrency, stale-lease recovery at start |
| Step context | `context.py` | The only interface roles get: log, evidence, sandboxed commands, budgets, cancellation checks |
| Roles | `roles.py` | Specialised, deterministic implementations with declared permissions (`ROLE_POLICIES`) |
| Repair engine | `repair.py` | Simulated coding agent: fault localisation → mutations → validation |
| Sandbox | `sandbox.py` | Command runner (timeout, cancel, process-tree kill), env allow-list, `Workspace` write guard, hermetic `Git` |
| API / dashboard | `api.py`, `static/index.html` | Read-only views of real state plus human actions |

## Workflow state machine

```
 queued ──claim──► running ──► awaiting_approval ──► approved
   │                 │  │  │          │           └─► rejected
   │                 │  │  └► blocked ─┼─retry─► queued
   │                 │  └──► failed ───┼─retry─► queued
   └───────────────► cancelled ◄───────┘ (from any non-terminal state)
                   running ──(stale lease)──► queued        crash recovery
```

Terminal states: `approved`, `rejected`, `cancelled`. `failed` and `blocked` keep their evidence and can be
retried by a human. Illegal transitions raise `IllegalTransition` and leave state unchanged (tested).

## Pipelines

Triage runs first and decides the work type; the remaining steps are then appended:

| Type | Steps after triage |
|---|---|
| bug | requirements → plan → workspace → reproduce → **[coding → testing → review → feedback]**ⁿ → quality_gate → approval |
| feature | requirements → plan → workspace → **[coding → …]** (blocks at coding: no LLM coding runtime) |
| support | support → answer_review → approval |

## Implementation loop (coding ↔ review)

```
            ┌──────────────────────────── revise: feedback items + required probes + rejected fingerprints ─┐
            ▼                                                                                               │
 reproduce ─► coding ──► testing ──────────► review ─────────────────────► feedback ──┬─ pass ──► quality_gate ─► approval
            (repair,    (repro, suite,       (≥2 independent adapters,     (aggregate,  │                (re-verify all,
             feedback-   earlier probes,      each gets its own copy of     verify       ├─ stalled ───┐   map acceptance
             aware)      scope/security)      diff+PRD+tests+history)       evidence,    ├─ exhausted ─┼─► BLOCKED (escalation)
                                                                            decide)      └─ unresolved ┘
```

Each pass is one **iteration**, stored in the `iterations` table. It has its own state machine:

```
coding → testing → reviewing → aggregating → revise | passed | stalled | exhausted | unresolved
   (any open phase → failed | cancelled; coding/reviewing → stalled | exhausted on escalation)
```

| Phase | What happens | Evidence persisted |
|---|---|---|
| coding | The repair engine starts from the repro-test commit (iteration 1 or `restart_from_checkpoint`) or from the previous iteration's commit (`build_on_previous`). Every candidate must pass the repro test, **every required probe from earlier feedback**, and the full suite. Candidates whose patch fingerprint was already rejected are skipped. Lines named in feedback are tried first. | patch, fingerprint, strategy, addressed feedback IDs |
| testing | Independent re-run of the repro test, full suite, all earlier probes, and the scope/security scan | test results per iteration |
| review | Each configured adapter reviews a deep copy of the same bundle and cannot see the others' output. A reviewer error is recorded as `error`, never as approval. Fewer than `min_reviewers` (2) completed reviews means a retryable failure. | one `reviews` row per reviewer: provider, model, `simulated`, verdict, findings, tokens, cost |
| feedback | Merges test findings and reviewer findings by location, **executes every reviewer check and probe**, classifies each claim's evidence (below), computes the failure signature and revision strategy, then decides | aggregated feedback items with evidence states, inspected reviews, decision, reason |

**Evidence states** (`loop.aggregate`). Reviewer claims are never trusted as text:

| State | Meaning | Effect |
|---|---|---|
| confirmed | executed failure whose expected value is grounded in an authoritative requirement (issue, acceptance criteria, docstrings, README: exact quoted value or ≥50% content-word match) or corroborated by a second reviewer's identical expectation | blocking |
| unsupported | executed failure, expectation not traceable to any requirement | unresolved |
| conflicting | reviewers expect different results for the same call | unresolved |
| unverified | model claim without an executable probe, or a probe that passes or is itself broken (`NameError`, `ImportError`, …) | downgraded to major |
| invalid | check rejected by validation (non-literal arguments, ambiguous function, unsafe probe) | recorded only |
| passed | executed and passing | recorded only |

A review counts as an *inspection* only if it did observable work on the change (valid executed checks on changed
functions, located findings, or a deterministic technique).

**Decision rules** (`loop.assess_progress`, applied in this order):

1. 0 blocking findings → **pass**, unless evidence is unsupported/conflicting or fewer than `min_reviewers` reviews
   inspected the change → **unresolved** (escalate; never a silent approval or an arbitrary block).
2. The patch fingerprint equals an earlier iteration's → **stalled** (repeated solution).
3. The blocking-finding signature equals the previous iteration's → **stalled** (identical failure pattern).
4. The blocking count did not decrease for `stall_window` consecutive iterations → **stalled** (no progress).
5. `max_iterations`, `max_loop_seconds`, `max_cost_usd` or `max_llm_tokens` reached → **exhausted**.
6. Otherwise → **revise**: the orchestrator re-arms the four loop steps, and the next iteration starts at coding.

`stalled`, `exhausted` and `unresolved` put the workflow in **blocked**, with an `escalation` record (reason, open blocking items,
recommended action). A human `retry` restarts the loop at coding and, if needed, grants one more iteration.

**Quality gate** re-verifies the final commit from scratch: repro test fails on base and passes now, full suite,
all review probes, scope and security, final iteration passed with 0 blocking findings, at least 2 independent
reviews **that inspected the change**, no unresolved evidence, **the reviewed commit is the final HEAD with a clean
worktree**, and budgets respected. It maps every PRD acceptance criterion to one of these checks; an unmappable
criterion fails the gate. Only `SUCCESS` reaches `awaiting_approval`, and `approve` refuses a code workflow
without a `SUCCESS` gate.

## Step execution and error taxonomy

Each step runs up to `max_attempts` times (coding: 3, others: 2). Each attempt is a row with status, error,
duration and detail. A role signals its outcome by raising:

| Signal | Meaning | Orchestrator action |
|---|---|---|
| return value | success | store output, merge into workflow `data`, next step |
| `TransientError`, `StepTimeout` | retryable (flaky tool, search tier exhausted, deadline) | exponential backoff (cancellable), next attempt |
| `BlockedError` | needs human input (cannot reproduce, unsupported work, no README) | workflow → `blocked` |
| `PermanentError` / other exception | non-retryable (verification or security failure, budget exhausted, bug) | workflow → `failed` with traceback in the log |
| `Cancelled` | cancellation flag observed | attempt → `cancelled`, workflow → `cancelled` |

Retries are not blind repeats. The coding role widens its search on each attempt (tier 1 → 3), so a
retry means "try harder," not "try the same thing again."

## Isolation and permissions

**Execution backends** (`systemfactory/execution.py`). Every execution of target-repository or model-written code
goes through `StepContext.run_python`, which delegates to the workflow's backend:

| Backend | Isolation | Use |
|---|---|---|
| `local` | process: host Python, scrubbed environment, worktree cwd | deterministic development and CI; trusted code only |
| `docker` | container: `--network none`, read-only root, worktree and probes mounted read-only, tmpfs `/tmp`, `--cap-drop ALL`, `no-new-privileges`, uid 65534, PID/memory/CPU limits, forced removal | real-LLM runtime (default), untrusted code |

A workflow with `require_isolation` (the real-LLM runtime's default) is **blocked without executing anything** when
Docker or the sandbox image is unavailable; it never falls back to `local`. Details and trust policy:
`sandbox/README.md`.

- **Worktree per workflow**: `git worktree add -B sf/wf-<id> data/worktrees/wf-<id> <base>`. The target repo's
  `main` is only read. Approval does not merge.
- **Write allow-lists**: `Workspace` rejects path escapes, `.git/`, and any path not matching the role's globs.
  QA may write only `tests/test_issue_*.py`; coding may write only the plan's `target_files`.
- **Scrubbed environment**: subprocesses get an allow-list of variables (PATH, SYSTEMROOT, TEMP, …). Anything
  named like KEY/TOKEN/SECRET/PASSWORD/AUTH is dropped. `PYTHONDONTWRITEBYTECODE=1`.
- **Hermetic git**: `GIT_CONFIG_GLOBAL` points at an empty file and `GIT_CONFIG_NOSYSTEM=1`, so the user's
  credentials helpers, hooks and aliases are never used. Commits use an agent identity.
- **Timeouts and cancellation**: every command has a timeout; while it runs, the runner polls the cancel flag
  and the step deadline and kills the process tree (`taskkill /T /F`) if either fires.
- **Scope/security scan** (every iteration, and again in the quality gate): rejects fixes that touch tests or out-of-plan files, or that add secrets. Flags dangerous
  calls (`eval`, `subprocess`, network) and large diffs.
- **Audit log**: every transition, attempt, retry, decision and actor is an `events` row.

**Limitations:** the `local` backend is process-level only and must not run untrusted code. The `docker` backend
shares the host kernel (not a VM). Git operations run on the host with a hermetic configuration; they read the
target's files but never execute them. See DECISIONS.md D5, D20, D21.

## Data model (SQLite)

| Table | Key columns |
|---|---|
| `issues` | title, body, source |
| `workflows` | issue_id, repo, kind, state, current_step, cancel_requested, options, budget, usage, data, branch, worktree, base_commit, error, lease_owner, heartbeat_at, started/finished |
| `steps` | workflow_id, seq, name, role, status, attempts, output, error, started/finished |
| `attempts` | step_id, n, status, error, detail, started/finished |
| `events` | workflow_id, ts, level, step, actor, message, data |
| `evidence` | workflow_id, step, kind (prd, plan, test_run, repair_search, patch, review, feedback, quality_gate, diff, support_answer), title, content, data |
| `approvals` | workflow_id, decision, actor, comment, ts |
| `iterations` | workflow_id, n, status (loop phase), base, commit, fingerprint, patch, tests, feedback, failure_signature, blocking_count, decision, decision_reason |
| `reviews` | workflow_id, iteration, reviewer, adapter, provider, model, model_digest, simulated, execution, verdict, summary, analysis, findings, checks, tokens, cost_usd, seconds, error, llm_call_id |
| `llm_calls` | every model call including failures: provider, execution (real-local / real-cloud / simulated), model, digest, status, timings, tokens reported by the runtime, sanitized prompt and response, hashes |
| `tool_calls` | every coding-agent action: tool, arguments, ok, result, duration, model call id |
| `test_runs` | every test command: purpose, command, commit, exit code, pass/fail/error counts, duration, output tail, execution backend |

Workflow `data.provenance` records the System Factory revision and dirty state **at submission**; evidence
manifests keep that separate from the revision that exported them.

## Budgets

Per workflow: `max_wall_seconds`, `max_candidate_evals`, `max_test_runs`, `max_iterations`, `max_loop_seconds`,
`max_llm_tokens`, `max_cost_usd`, `stall_window`. Usage counters (`test_runs`, `candidate_evals`, `llm_tokens`,
`cost_usd`) are incremented where the work happens. Token counts come from the provider's response (Ollama
`prompt_eval_count` / `eval_count`); they stay 0 in the simulated runtime. LLM budget overruns escalate (blocked);
test and candidate budget overruns fail.

## Concurrency

Workers claim with one `BEGIN IMMEDIATE` transaction (select the oldest queued → set running + lease). Git
operations that write shared refs are serialised by a lock. A test runs 3 competing workers over 3 workflows
and asserts each one is claimed exactly once.
