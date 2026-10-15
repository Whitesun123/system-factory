# System Factory

A local, AI-assisted software-engineering orchestrator. It takes an issue (bug, feature request or support
question) through **Intake → Triage → PRD → Plan → isolated workspace → Reproduction → [Coding → Deterministic
tests → Independent reviewers → Evidence verification → Feedback aggregation → Revision]ⁿ → Quality gate →
Human approval**. It never merges, pushes or deploys.

Two runtimes share one pipeline:

| Runtime | Coding agent | Reviewers | Use |
|---|---|---|---|
| `simulated` (default) | deterministic search-based program repair | two deterministic reviewers (spec examples, diff heuristics) | unit tests, CI, offline demo |
| `ollama` | real LLM tool-using agent (local Ollama) | two real LLM reviewers, A and B, using different models on **one** provider | real local inference |

See [MILESTONE5_STATUS.md](MILESTONE5_STATUS.md) for what has and has not been verified.

## Architecture in one paragraph

A deterministic orchestrator ([orchestrator.py](systemfactory/orchestrator.py)) owns the state machine,
retries, budgets, cancellation and the approval gate. Roles ([roles.py](systemfactory/roles.py)) do the work
inside a step through a restricted context: they can only write allow-listed files in their own git worktree and
run fixed commands with a scrubbed environment. The implementation loop repeats coding, testing, review and
feedback per **iteration**. Reviewer findings are trusted only with executable evidence: probes and
reviewer-proposed specification checks are run in the sandbox. Unconfirmed blockers are downgraded, and checks
that fail when executed become blocking. The loop stops on success, on a stall (repeated patch, identical failures,
no progress) or on a budget (iterations, time, tokens, cost). A stall or exhausted budget escalates to a human.
Details: [ARCHITECTURE.md](ARCHITECTURE.md) and [DECISIONS.md](DECISIONS.md).

## Install (Windows 11, PowerShell)

Requirements: Python 3.11+ and git. Ollama is optional; it's only needed for the `ollama` runtime.

```powershell
git clone https://github.com/Whitesun123/system-factory.git
```

```powershell
cd system-factory; .\scripts\setup.ps1
```

`setup.ps1` creates `.venv`, installs `requirements.txt` and generates the separate demo repository in `data/`.
Without the script: `pip install -r requirements.txt`, then `python -m systemfactory init-demo`.

## Start the dashboard

```powershell
.\scripts\start.ps1
```

Open http://127.0.0.1:8765. The API, dashboard and background worker bind to localhost only. There is no
authentication, so don't expose it.

The dashboard shows the runtime and provider status, installed models with digests, a runtime selector, each
workflow's pipeline, an **iterations** view (patch, tests, every reviewer report, executed checks, aggregated
feedback, decision), an **agents** view (every model call with tokens and timing, the sanitized prompt and response,
every coding-agent tool call, every test command with exit code and pass/fail counts), the diff, the audit log,
approval controls and **Export evidence (.zip)**.

## Configure Ollama (local LLM runtime)

1. Install Ollama from https://ollama.com/download.
2. Optional: store models on a large drive by setting `OLLAMA_MODELS` (for example `D:\OllamaModels`) for the
   Ollama server process.
3. Start the server, localhost only, with cloud features disabled:

   ```powershell
   $env:OLLAMA_NO_CLOUD = "1"; ollama serve
   ```

4. Download the default models (about 25 GB):

   ```powershell
   .\scripts\pull_models.ps1
   ```

   That runs `ollama pull qwen2.5-coder:14b`, `ollama pull phi4:14b` and `ollama pull mistral-nemo:12b`.
5. Check availability. This reads the model list only; it never loads a model:

   ```powershell
   python scripts\check_models.py
   ```

System Factory talks to Ollama over its HTTP API (`POST /api/chat` with `stream: false` and a JSON-schema `format`
for structured output; `GET /api/tags` for model digests; `GET /api/version`). Temperature is 0, and the seed is
fixed per attempt.

### Configuration

All settings are environment variables; [.env.example](.env.example) lists them. Defaults are project-relative.

| Variable | Default | Meaning |
|---|---|---|
| `SF_RUNTIME` | `simulated` | default runtime for new workflows (`simulated` or `ollama`) |
| `SF_DATA_DIR` | `./data` | SQLite DB, generated demo repo, worktrees, evidence |
| `SF_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server |
| `SF_CODER_MODEL` | `qwen2.5-coder:14b` | coding agent |
| `SF_REVIEWER_A_MODEL` | `phi4:14b` | Reviewer A: requirements, correctness, architecture |
| `SF_REVIEWER_B_MODEL` | `mistral-nemo:12b` | Reviewer B: edge cases, regressions, security, maintainability |
| `SF_OLLAMA_TIMEOUT` | `300` | seconds per model call |
| `SF_OLLAMA_NUM_CTX` / `SF_OLLAMA_KEEP_ALIVE` | `12288` / `10m` | context window / how long a model stays loaded |
| `SF_CODER_MAX_TURNS` | `6` | tool-loop turns per coding attempt |
| `SF_REVIEWERS` | `simulated-spec,simulated-diff` | reviewers for the simulated runtime |
| `ANTHROPIC_API_KEY`, `SF_ALLOW_PAID_INFERENCE=1` | unset | enable the optional, paid Claude reviewer adapter |

Per-workflow budgets (iterations, loop time, tokens, cost, test runs) are in `Settings.default_budget`
([config.py](systemfactory/config.py)) and can be overridden on submit (`--max-iterations`).

## Run workflows

Simulated, fully offline:

```powershell
python -m systemfactory demo
```

Real local inference (needs Ollama and the models):

```powershell
python -m systemfactory submit --sample checkout --runtime ollama
```

```powershell
python -m systemfactory run
```

```powershell
python -m systemfactory show 1
```

Or pick **Real local inference (Ollama)** in the dashboard. Scripted real scenarios A–E (success, revision after
review, stall via feedback ablation, provider unavailable, approval guard), checked against an oracle the agents
never see:

```powershell
python scripts\real_demo.py
```

| CLI | Purpose |
|---|---|
| `submit --sample S [--runtime simulated\|ollama] [--no-coder-feedback] [--max-iterations N]` | Submit a sample issue. Samples: shipping, pagination, bulk, checkout, coupon, not_reproducible, feature, support |
| `submit --title T --body B` | Submit a custom issue (bugs need a fenced `>>>` example) |
| `run` / `status` / `show ID` | Process the queue / list workflows / show steps, iterations, reviewers, evidence |
| `approve ID --by NAME` / `reject ID --by NAME` | Human decision (approval requires quality gate SUCCESS) |
| `cancel ID` / `retry ID` | Cancel / retry a failed or blocked workflow |
| `export ID [--out DIR]` | Write a sanitized evidence package |
| `runtime` | Show configured runtime and provider/model availability |
| `serve` | API, dashboard and worker |

## Logs and evidence

Everything is persisted in SQLite (`data/systemfactory.db`): workflows, steps, attempts (with iteration), iterations,
reviews (provider, model, digest, real or simulated, findings, checks), `llm_calls` (status, timings, tokens reported
by the runtime, sanitized prompt and response), `tool_calls`, `test_runs` (command, commit, exit code, pass/fail
counts), events (audit log), evidence and approvals.

`python -m systemfactory export ID` (or the dashboard button) writes `summary.md`, `run-manifest.json`,
`iterations.json`, `review-findings.json`, `test-results.json`, `quality-gate.json`, `patch.diff`, `llm-calls.json`,
`tool-calls.json` and `events.json` to `data/evidence/wf-ID/`. Secrets are redacted and local paths are rewritten
to `<PROJECT>`, `<DATA_DIR>` or `<HOME>`.

## Tests

```powershell
.\scripts\test.ps1
```

The suite is deterministic and needs no Ollama, network or LLM. Provider-failure paths are tested against a
closed local port. Real-inference runs are in `scripts/real_demo.py` and `scripts/eval_reviewers.py` and are not
part of the suite.

## What remains unverified

See [MILESTONE5_STATUS.md](MILESTONE5_STATUS.md). In short: real-LLM scenarios B–E have **not** been run end to end
with the final code; the reviewer-check mechanism was benchmarked on 5 labeled patches only; the default Reviewer B
model scored lower than phi4 in that benchmark; and isolation is process-level, not a container.

## Repository layout

```
systemfactory/   orchestrator, statemachine, store, roles, loop (aggregation/stall rules), repair (simulated coder),
                 coders (Ollama coding agent), reviewers (simulated, Ollama, Claude, command adapters),
                 llm (Ollama client + call recorder), evidence (export), sandbox, api, cli, static/ (dashboard)
demo/shopcart_template/   intentionally buggy demo library (copied into data/ as its own git repo)
scripts/         setup/start/test/pull_models (PowerShell), check_models, real_demo, eval_reviewers
tests/           deterministic unit and end-to-end tests
```

Documents: [ARCHITECTURE.md](ARCHITECTURE.md), [DECISIONS.md](DECISIONS.md), [DEMO.md](DEMO.md),
[PRESENTATION.md](PRESENTATION.md), [MILESTONE5_STATUS.md](MILESTONE5_STATUS.md).
