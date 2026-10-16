# Presentation Notes

## The 2-minute explanation

> **What it is.** System Factory takes an incoming issue (bug, feature request or support question) through the
> stages a good engineering team uses: triage, requirements, plan, an isolated workspace, a failing reproduction
> test, then a loop of **coding → deterministic tests → independent review → evidence verification → feedback →
> revision**, a final quality gate, and a human decision. It never merges, pushes or deploys.
>
> **The key design choice.** The *control flow* is not AI. A deterministic orchestrator owns the state machine:
> which step runs next, retries, iteration limits, time/token/cost budgets, cancellation and the approval gate.
> Model-backed work happens only *inside* a step, through a narrow interface with explicit permissions. The
> orchestrator decides what happens next; a model never does.
>
> **How we know a fix is real.** The QA role turns the issue's example into a test and proves it **fails** on the
> current code. A patch counts only when that test passes, the whole suite passes, and at least two independent
> reviewers have no *verified* blocking finding. Reviewer claims are not trusted as text: reviewers propose
> executable checks, and System Factory runs them in a Docker sandbox. A failing check is a confirmed defect only
> if its expected value traces to the issue, acceptance criteria or docs; it then becomes feedback for the next
> revision. Untraceable or conflicting evidence escalates to a human instead of blocking or approving.
>
> **How it stops.** Either there are no verified blocking findings and the quality gate re-verifies every
> acceptance criterion, or the loop detects it is stuck (same patch, same failures, no progress) or hits a budget
> and escalates to a human as *blocked*.
>
> **What's real today.** Two runtimes share one pipeline. The **deterministic runtime** is fully tested and in CI.
> The **Ollama runtime** (a tool-using LLM coder and two LLM reviewers, everything executed in a Docker sandbox) ran
> scenarios A-E twice on clean clones, including the real revision-after-review case, with results checked by hidden
> oracles. Those are two small synthetic bugs at fixed seeds, not an automation rate. Three Ollama models are
> **one provider**, and the docs and dashboard say so.

## What is real, what is simulated, what is unverified

| Category | Components | Evidence |
|---|---|---|
| **A. Deterministic runtime (verified)** | Orchestrator, state machine, iterations, stall detection, budgets, worktree isolation, quality gate, approval guard; search-based repair coder; `simulated-spec` and `simulated-diff` reviewers | Offline test suite on Windows and Ubuntu CI; sanitized evidence in `examples/evidence/` |
| **B. Real local Ollama runtime (validated on two synthetic bugs)** | `qwen2.5-coder:14b` coder; Reviewer A `phi4:14b`, Reviewer B `qwen3:14b`; Docker sandbox | Scenarios A-E, two runs x repeat 2 on clean clones (`examples/evidence/real/`): A 4/4, B 4/4 revise then pass, C 2/2 stalled, D failures recorded, E guard held |
| **C. Measured** | Reviewer evaluation: 23 labelled cases, 30.4 min of real inference | Raw verdicts caught 0-2 of 9 defects; the evidence pipeline caught 6 of 9 with the selected panel and 0 false blocks (`evaluation/results/REPORT.md`; optimistic, because the pipeline fixes came from the same set) |
| **D. Not done** | An external issue set and automation rate, a second provider (needs approval), semantic grounding, dependency installation in the sandbox, PostgreSQL | — |

## The orchestrator and the LLM roles

- The **orchestrator** is plain Python. It owns ordering, attempts, iterations, budgets, the decision rules
  (`loop.assess_progress`) and every state transition. It is identical in both runtimes.
- A **role** receives a `StepContext`. It can write only allow-listed files in its own worktree and run fixed
  commands with a scrubbed environment. It reports by returning output or raising `Transient`, `Blocked` or
  `Permanent`.
- The **LLM coder** is one such role. It chooses one JSON action per turn (`list_files`, `read_file`, `run_tests`,
  `submit`). It has no shell, its edits are limited to the plan's target files, and every action is a `tool_calls`
  row.
- The **LLM reviewers** are adapters behind the same `Reviewer` interface as the deterministic ones. Each gets a
  deep copy of the same bundle (diff, requirements, test evidence, history) and cannot see the other's output.
- **Evidence verification** belongs to the orchestrator side, not to a model. Tests, probes and reviewer checks are
  executed; model-written probes are statically vetted first.

## Why review happens *between* coding iterations

The original design (Milestone 3) reviewed once, at the end, and retried coding blindly on failure. Tests alone
passed a fix that was incomplete.

- **Benefit:** a defect found in review becomes specific, executable feedback (a failing check) for the *next*
  revision, instead of a rejection at the very end. Required probes accumulate, so a later revision cannot
  silently undo an earlier fix. Repeated patches are detected by fingerprint.
- **Cost:** every iteration pays for a test run and at least two reviews. In the deterministic bulk case that's
  about 23 test runs instead of about 12. With real models it's about 4–8 extra model calls per iteration.
- **Limitations:**
  - A reviewer's expected value can be wrong. A wrong check produces a false block, which surfaces as a stall or
    escalation rather than a bad merge, but it still costs a human's time.
  - Small local models skip review procedures unless forced into structured output. That is why checks are a
    required schema field.
  - Review quality depends heavily on the model: 3/5 to 5/5 on a 5-case benchmark, which is too small to
    generalise from.

## How to explain any decision (the four-beat pattern)

1. **Problem:** "We needed X, and the risk was Y."
2. **Solution:** "So we did Z."
3. **Tradeoff:** "That costs us A. We accepted it because B."
4. **Evidence:** "And we know it works because test or demo C shows it."

Example (D18): "A reviewer said a patch was fine, and another time a reviewer invented a blocker. **So**
reviewer claims only count when an executed check demonstrates them. **That costs** an extra sandboxed run per
check, and a wrong expected value can still block, but that escalates to a human instead of merging. **And** the
benchmark showed the probe rule turning two false blockers into non-blocking notes, and a confirmed check catching
an incomplete fix the model itself had labelled minor."

## Likely questions from a technical reviewer

**"Is this actually using AI?"**
Optionally. The default and CI runtime is deterministic. The Ollama runtime calls real local models, and every call
is recorded with model, digest, tokens reported by Ollama and timing. Scenarios A-E ran with real models on the final
code (`examples/evidence/real/`).

**"Are these really independent reviewers?"**
They are independent in input (deep-copied bundle, no shared output) and in technique or model. They are **not**
provider-diverse: two models on Ollama are one runtime, and the label says exactly that. Claude (official SDK) and a
generic command adapter exist for a second provider but are inactive without keys and approval.

**"What stops an agent from cheating, for example by editing the test?"**
The coder's workspace can write only the planned source files, so a write to `tests/` is refused as a tool error (a
test exercises this on the LLM path). The scope scan fails any fix diff that touches tests, every iteration and again
in the quality gate. The reproduction test is committed before coding starts. Approval is refused without a gate
SUCCESS.

**"Is it safe to run on untrusted repositories?"**
Safer, not safe. In the real runtime every test, probe and check runs in a fresh container: no network, a read-only
root, read-only mounts, all capabilities dropped, a non-root user, and PID, memory and CPU limits. If Docker is
unavailable the workflow is blocked before anything runs. Limits: containers share the host kernel, git runs on the
host, and dependency installation from the target repository is not supported. The deterministic runtime still uses
host processes, for trusted code and CI only (D21).

**"The API has no authentication?"**
Correct, it is a single-user local tool. It binds to 127.0.0.1, refuses non-local Host headers (DNS rebinding) and
refuses cross-origin state-changing requests (CSRF).

**"What's the automation rate?"**
Not claimed. A real rate needs an external issue set and repeated real runs.

**"How did you choose the reviewer models?"**
From a labelled evaluation of 23 cases (D23, `evaluation/results/REPORT.md`). Raw model verdicts caught almost
nothing; executing the models' checks did. The evaluation also found three defects in our own evidence pipeline,
which we fixed and then re-scored without new inference. Because the fixes came from the same dataset, the final
numbers are optimistic, and we say so.

**"Why SQLite and not Postgres?"**
Postgres wasn't needed yet, and the store is one class. SQLite in WAL mode with atomic claims is tested under
concurrent workers.

**"How do you stop it looping forever?"**
Repeated fingerprint, identical failure signature, no decrease in blocking findings over two iterations, and hard
budgets (iterations, loop time, tokens, dollars). Any of these sets the workflow to *blocked* with an escalation
record.

## Historical note (Milestone 4)

Until Milestone 4 the coder and reviewers were deterministic only, and the presentation said "no model runtime is
configured". That remains true of the default runtime. The Ollama runtime was added in Milestone 5.
