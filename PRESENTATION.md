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
> proves it **fails** on the current code. Then an implementation loop runs in an isolated git worktree:
> code, run the tests, and have **at least two independent reviewers** check the actual diff against the
> requirements and the test evidence. Their findings are merged into specific, evidence-backed feedback, and
> each behavioural claim is re-executed as a probe so a made-up finding can't block the fix. The feedback goes back
> to the coder for the next revision.
>
> **How it stops.** The loop ends in one of two ways. Either there are no blocking findings, and an independent
> quality gate re-verifies every acceptance criterion. Or it detects that it is stuck (the same patch again, the
> same failures, or no progress) or hits its iteration, time or cost budget, and escalates to a human as
> *blocked*. A human approval is always required, and approval never merges.
>
> **What's real today.** The orchestrator, the loop, stall detection, budgets, worktree isolation, the quality
> gate and the dashboard are real and covered by 46 tests. The coder and both reviewers are honestly
> *simulations*: deterministic techniques, not LLMs, because no model runtime is configured. The two reviewers are
> independent by technique (one executes documented examples, one analyses the diff), not by model. Adapters for
> Claude and for any other provider exist and switch on with keys and budget approval.
>
> **What's next.** A real coding provider that consumes the same feedback items, real multi-provider review,
> containers, and PostgreSQL.

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
Three layers. The coding role's workspace can write only the planned source files. The scope scan, which runs every iteration and again in the quality gate, fails
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

**"Isn't review too late? What if the first fix is wrong?"**
That's what the loop is for. In the bulk-discount demo the first fix passes every test but fails review: a
documented example of the same function is still wrong. The finding is confirmed by re-running it, both reviewers
point at the same function, and the coder gets the exact failing call as a required probe. Iteration 2 fixes it.

**"Are these really two different models?"**
No, and the dashboard says so. Both are deterministic reviewers using different techniques. Each review row records
provider, model and a `simulated` flag, and "multi-provider" is shown only when two non-simulated providers actually
completed reviews. Claude runs through the official SDK, and any other provider plugs in via the command adapter.

**"How do you stop it looping forever and burning money?"**
Four stop rules: a repeated patch fingerprint, an identical failure signature, no decrease in blocking findings over
two iterations, and hard budgets (iterations, loop time, tokens, dollars). Any of them sets the workflow to
*blocked*, with an escalation record that says why and what is still open.

