# System Factory

A local, AI-assisted software-engineering workflow system:

**Intake → Triage → PRD → Plan → Coding → Testing → Review → Human Approval**

It handles bug reports, feature requests and support questions. A deterministic orchestrator drives each
issue through a pipeline of specialised roles, isolates code changes in git worktrees, records every
attempt, log line and piece of evidence in a persistent store, and **always stops for a human decision**.
It never merges, pushes or deploys.

## Status (honest)

| Area | Status |
|---|---|
| Orchestrator, state machine, retries, cancellation, budgets, crash recovery | **Working**, tested |
| Persistent state (SQLite): workflows, steps, attempts, events, evidence, approvals | **Working**, tested |
| Bug → failing reproduction test → fix in isolated worktree → re-test → review → approval | **Working** end to end on a real demo repo |
| Coding agent | **Simulated**: deterministic search-based program repair. **No LLM is called.** |
| Triage / PRD / plan / review / support roles | **Rule-based and template-based** (deterministic), labelled as such |
| Dashboard over real state (no fake metrics) | **Working** |
| Claude API / Agent SDK runtime | **Not implemented.** No API key configured; requires budget approval |
| Container sandbox (Docker) | **Not yet.** Docker is installed but the daemon is not running; process-level isolation is used |
| PostgreSQL | **Not yet.** Not installed; SQLite behind the `Store` class |

The ~95% automation figure in the brief is a long-term goal. Nothing here measures or claims it.

## Quick start (Windows, PowerShell or Git Bash)

Requirements: Python 3.11+ with `fastapi`, `uvicorn`, `pytest`, `httpx` (all already present on this machine), and git.

```bash
python -m systemfactory demo
```

That creates the separate buggy demo repository (`data/repos/shopcart`), submits every sample issue,
runs them all and prints a summary. To use the dashboard:

```bash
python -m systemfactory serve
```

Then open http://127.0.0.1:8765.

Run the test suite (about 75 seconds; it creates real git worktrees and runs real pytest):

```bash
python -m pytest -q
```

## CLI

| Command | Purpose |
|---|---|
| `init-demo [--force]` | Create the demo target repository (kept as-is if it exists) |
| `submit --sample shipping\|pagination\|coupon\|not_reproducible\|feature\|support` | Submit a sample issue |
| `submit --title T --body B [--fault STEP=N] [--throttle-ms MS]` | Submit a custom issue, optionally with injected faults |
| `run` | Process the queue until idle |
| `status` / `show ID` | List workflows / show steps, events and evidence |
| `approve ID --by NAME [--comment C]` / `reject ID --by NAME` | Human decision |
| `cancel ID` / `retry ID` | Cancel (queued or running) / retry a failed or blocked workflow |
| `serve [--port 8765]` | API + dashboard + background worker (localhost only) |
| `runtime` | Show which agent runtime is active |

## Layout

```
systemfactory/
  orchestrator.py   deterministic workflow driver + Worker (leasing, concurrency)
  statemachine.py   workflow/step states, legal transitions, per-type pipelines
  store.py          SQLite persistent state and audit log
  context.py        what a role may use: logging, evidence, sandboxed commands, budgets
  roles.py          triage, requirements, architecture, infra, qa, coding, security, review, support
  repair.py         search-based program repair (the simulated coding agent)
  sandbox.py        scrubbed env, timeouts, cancellation, workspace write guard, hermetic git
  api.py, static/   FastAPI service and dashboard
  demo.py, cli.py   demo repo setup, sample issues, CLI
demo/shopcart_template/   source of the intentionally buggy demo repository
tests/                    unit + end-to-end tests
data/                     (git-ignored) SQLite DB, generated demo repo, worktrees
```

## Documents

- [ARCHITECTURE.md](ARCHITECTURE.md): components, state machine, data model, isolation model
- [DECISIONS.md](DECISIONS.md): each major choice as problem → solution → tradeoff → evidence
- [DEMO.md](DEMO.md): 5-minute demo script
- [PRESENTATION.md](PRESENTATION.md): 2-minute explanation and likely questions
- [SYSTEM_FACTORY_HANDOFF.md](SYSTEM_FACTORY_HANDOFF.md): the original brief

## Next milestones

1. **Real LLM coding provider** (Claude API / Agent SDK) behind the same role interface and permission
   policies, with token/cost metering into the existing `usage` budget. Needs an API key and budget approval.
2. **Container sandbox**: run agent commands in Docker (no network, read-only base, worktree mounted).
3. **PostgreSQL** store implementation (same `Store` interface) when multi-process deployment is needed.
4. Richer QA: coverage-guided fault localisation, mutation testing of the generated test.
