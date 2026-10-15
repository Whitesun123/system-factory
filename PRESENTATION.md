# Presentation Notes

## The 2-minute explanation

> **What it is.** System Factory takes an incoming issue (a bug, a feature request or a support question)
> and moves it through the same stages a good engineering team uses: triage, requirements, plan, code,
> test, review, and then a human sign-off.
>
> **The key design choice.** The *control flow* is not AI. A small deterministic orchestrator owns the state
> machine: which step runs next, how many retries, time and compute budgets, cancellation, and the approval
> gate. AI-style work happens only *inside* a step, through a narrow interface with explicit permissions. That
> makes the system testable and auditable, and it guarantees a human always gets the final say.
>
> **How we know a fix is real.** For a bug, the QA role turns the example from the issue into a test and
> proves it **fails** on the current code. Only then does coding start, in an isolated git worktree. A fix
> counts only if that test now **passes** and the whole existing suite still passes. Every run, diff and
> decision is stored as evidence.
>
> **What's real today.** The orchestrator, state store, worktree isolation, retries, cancellation, security
> gate, approvals and dashboard are real and covered by 22 tests. The coding agent is honestly a
> *simulation*: a search-based repair engine, not an LLM, because no Claude API runtime is configured
> yet. It fixes simple bugs. When it can't, it fails or hands off openly instead of faking it.
>
> **What's next.** Plug a Claude provider into the same role interface, run agents in containers, and move the
> store to PostgreSQL.

## How to explain any decision (the four-beat pattern)

Use the same four beats every time. It keeps answers short and confident:

1. **Problem:** "We needed X, and the risk was Y."
2. **Solution:** "So we did Z."
3. **Tradeoff:** "That costs us A. We accepted it because B."
4. **Evidence:** "And we know it works because test or demo C shows it."

Example (D4): "We needed agents to change code without ever touching main. **So** every workflow gets its own
git worktree and branch. **That costs** some branch clutter, which is fine because the branches are the audit
trail. **And** the end-to-end test checks that main's commit hash is identical before and after approval."

Tips: lead with the problem, not the technology. Say the tradeoff out loud before someone asks for it. End on
evidence, not adjectives.

## Likely questions from a technical reviewer

**"Is this actually using AI?"**
Not yet for coding, and the UI says so in the header. The runtime is a deterministic simulation. The architecture
is built so that an LLM provider drops into the role interface and reports tokens and cost into the existing
budget counters. A Claude Code subscription is a development tool; an application runtime needs Claude API
access and an approved budget.

**"What's the automation rate?"**
We don't claim one. The brief's ~95% is a long-term goal. Right now we can show which sample cases end where:
2 bugs fixed and verified, 1 bug beyond the simulated agent, 1 unreproducible report, 1 feature handed off,
1 support answer drafted. Measuring a real rate needs a real issue set and a real LLM runtime.

**"What stops an agent from cheating, for example by editing the test?"**
Three layers. The coding role's workspace can write only the planned source files. The security gate fails
the workflow if any test file changed in the fix diff. And the reproduction test is committed *before*
coding, so the review compares against it. There's a test that simulates a tampering agent and checks the
gate rejects it.

**"Is it safe to run code from an issue?"**
Only partly, and we document that. Today it's process-level isolation: secrets stripped from the environment,
writes confined to the worktree, timeouts. That does not stop malicious code from reading files or using
the network. Containers are the next milestone, and until then it should only run trusted issues.

**"Why SQLite and not Postgres?"**
Postgres isn't installed here, and the store is one class. SQLite in WAL mode with atomic claims is tested
under concurrent workers. Swapping it out is an implementation change, not a redesign.

**"What happens if it crashes mid-run?"**
Workers heartbeat. On restart, workflows with a stale lease go back to the queue, and their interrupted
attempt is recorded as `interrupted`. Steps reset their worktree to the last checkpoint commit before
rerunning.
