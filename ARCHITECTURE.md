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
             feedback-   earlier probes,      each gets its own copy of     confirm      ├─ stalled ─┐     map acceptance
             aware)      scope/security)      diff+PRD+tests+history)       probes,      └─ exhausted┴─► BLOCKED (escalation)
                                                                            decide)
```

Each pass is one **iteration**, stored in the `iterations` table. It has its own state machine:

```
coding → testing → reviewing → aggregating → revise | passed | stalled | exhausted
   (any open phase → failed | cancelled; coding/reviewing → stalled | exhausted on escalation)
```

| Phase | What happens | Evidence persisted |
|---|---|---|
| coding | The repair engine starts from the repro-test commit (iteration 1 or `restart_from_checkpoint`) or from the previous iteration's commit (`build_on_previous`). Every candidate must pass the repro test, **every required probe from earlier feedback**, and the full suite. Candidates whose patch fingerprint was already rejected are skipped. Lines named in feedback are tried first. | patch, fingerprint, strategy, addressed feedback IDs |
| testing | Independent re-run of the repro test, full suite, all earlier probes, and the scope/security scan | test results per iteration |
| review | Each configured adapter reviews a deep copy of the same bundle and cannot see the others' output. A reviewer error is recorded as `error`, never as approval. Fewer than `min_reviewers` (2) completed reviews means a retryable failure. | one `reviews` row per reviewer: provider, model, `simulated`, verdict, findings, tokens, cost |
| feedback | Merges test findings and reviewer findings by location, marks corroboration, **re-executes every reviewer probe** (an unconfirmed blocking finding is downgraded to major), computes the failure signature and revision strategy, then decides | aggregated feedback items, decision, reason |

**Decision rules** (`loop.assess_progress`, applied in this order):

1. 0 blocking findings → **pass**.
2. The patch fingerprint equals an earlier iteration's → **stalled** (repeated solution).
3. The blocking-finding signature equals the previous iteration's → **stalled** (identical failure pattern).
4. The blocking count did not decrease for `stall_window` consecutive iterations → **stalled** (no progress).
5. `max_iterations`, `max_loop_seconds`, `max_cost_usd` or `max_llm_tokens` reached → **exhausted**.
6. Otherwise → **revise**: the orchestrator re-arms the four loop steps, and the next iteration starts at coding.

`stalled` and `exhausted` put the workflow in **blocked**, with an `escalation` record (reason, open blocking items,
recommended action). A human `retry` restarts the loop at coding and, if needed, grants one more iteration.

**Quality gate** re-verifies the final commit from scratch: repro test fails on base and passes now, full suite,
all review probes, scope and security, final iteration passed with 0 blocking findings, at least 2 independent
reviews, and budgets respected. It maps every PRD acceptance criterion to one of these checks; an unmappable
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

**Limitation:** this is process-level isolation on the host, not a container. Example code from an
issue *is executed* during reproduction. See DECISIONS.md D5.

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
| `reviews` | workflow_id, iteration, reviewer, adapter, provider, model, simulated, verdict, summary, findings, tokens, cost_usd, seconds, error |

## Budgets

Per workflow: `max_wall_seconds`, `max_candidate_evals`, `max_test_runs`. Usage counters
(`test_runs`, `candidate_evals`, `llm_tokens`, `cost_usd`) are incremented where the work happens. The LLM
counters stay at 0 because no LLM is called. They exist so that a real provider has somewhere to report.

## Concurrency

Workers claim with one `BEGIN IMMEDIATE` transaction (select the oldest queued → set running + lease). Git
operations that write shared refs are serialised by a lock. A test runs 3 competing workers over 3 workflows
and asserts each one is claimed exactly once.
