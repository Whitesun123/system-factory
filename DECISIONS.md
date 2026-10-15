# Decisions

Each entry gives the **problem**, the **solution**, the **reason and tradeoff**, and the **verification evidence**.
Test names refer to files in `tests/`.

---

## D1. Deterministic orchestrator; AI only inside roles

- **Problem:** An agent that decides its own control flow is hard to test, audit, budget or stop. Approvals
  and retries must be guaranteed, not just likely.
- **Solution:** A plain-Python state machine owns ordering, retries, deadlines, budgets, cancellation and the
  approval gate. Roles only do work inside one step and report success or failure by returning or raising.
- **Reason / tradeoff:** It is predictable, testable and explainable. The cost is that pipelines are fixed per
  work type, so an agent cannot invent new steps. That is the intended choice for a system that changes code.
- **Evidence:** `test_legal_and_illegal_transitions`, `test_store_rejects_illegal_transition_and_keeps_state`,
  and every workflow test asserting exact attempt sequences.

## D2. SQLite now, behind a `Store` class; PostgreSQL later

- **Problem:** Workflow state must survive restarts and be safe under concurrent workers. PostgreSQL is
  preferred, but it is not installed and the Docker daemon is not running on this machine.
- **Solution:** SQLite in WAL mode, with every state change in a short `BEGIN IMMEDIATE` transaction. All SQL
  lives in `store.py`.
- **Reason / tradeoff:** No install, no service and no disk cost (C: is 97% full). Concurrent writes are
  serialised, which is fine at this scale. Moving to PostgreSQL means reimplementing one class.
- **Evidence:** `test_claim_is_exclusive_and_skips_cancelled`, `test_concurrent_workers_do_not_double_process`,
  `test_recover_stale_requeues_and_resets_running_step`.

## D3. The coding agent is a labelled simulation: search-based program repair

- **Problem:** No Claude API key is configured. A Claude Code subscription is a development assistant, not an
  application runtime. Faking LLM output would make the demo dishonest.
- **Solution:** A real, deterministic automated-program-repair loop. It localises the fault to the functions
  named in the issue's example (plus their callees), generates single-token mutations, and accepts a
  candidate only if the reproduction test **and** the full suite pass. Every UI label, commit message and
  output says "simulated, not an LLM."
- **Reason / tradeoff:** It genuinely fixes a class of bugs (wrong operator, off-by-one) and produces real
  evidence. It cannot implement features or multi-token fixes, and it says so: the feature request goes to
  `blocked` and the coupon bug fails after 3 honest attempts. An LLM provider can later implement the same
  role signature.
- **Evidence:** `test_bug_to_verified_fix_then_human_approval` (`>` → `>=`),
  `test_failed_attempt_is_retried_with_wider_search` (drops `- 1` on attempt 2),
  `test_unfixable_bug_exhausts_retries_and_fails_with_evidence`, `test_feature_request_blocks_honestly_at_coding`.

## D4. One git worktree and branch per workflow; never merge

- **Problem:** Agents must not touch the main checkout, and concurrent workflows must not collide.
- **Solution:** `git worktree add -B sf/wf-<id>` from the base commit. The test commit and fix commit land on that
  branch only. Approval records a decision and logs that the branch is ready for a human to merge.
- **Reason / tradeoff:** Worktrees are cheap and use real git, with no copies. Branches accumulate until cleaned
  up, which is acceptable because they are the audit trail.
- **Evidence:** `test_bug_to_verified_fix_then_human_approval` asserts `main` is unchanged before and after approval.

## D5. Process-level isolation now; containers next

- **Problem:** Agents run commands, and reproducing a bug means **executing example code taken from the issue**.
  That is untrusted input.
- **Solution (current):** An allow-listed environment (secrets dropped), hermetic git config, cwd fixed to the
  worktree, per-role write allow-lists, timeouts and process-tree kill.
- **Reason / tradeoff:** Docker is installed but its daemon is not running, and building a container image costs
  disk. Process isolation does **not** stop code that deliberately reads the filesystem or uses the network.
  It is acceptable only for trusted, local demo issues. Next step: run commands in a container with no
  network, the worktree mounted, and a read-only base.
- **Evidence:** `test_scrubbed_env_drops_secrets_and_unknown_vars` (child process cannot see `ANTHROPIC_API_KEY`),
  `test_workspace_blocks_escape_git_and_unlisted_writes`, `test_run_command_timeout_and_cancel`.

## D6. "Fixed" means fail-before / pass-after, plus the full suite

- **Problem:** Without a precise definition of "fixed," an agent can claim success or overfit.
- **Solution:** QA turns the issue's `>>>` example into `tests/test_issue_<n>.py`. It must **fail on the base
  commit** (or the workflow blocks as "cannot reproduce"). It is committed before coding starts. The fix must make
  it pass and keep the baseline suite green.
- **Reason / tradeoff:** It is objective and machine-checkable. It still checks only one example, so a human
  reviewer must judge generality. The plan's `risks` field says so.
- **Evidence:** `test_unreproducible_bug_blocks`. The `test_run` evidence records exit codes on base and on fix.

## D7. Typed failures and meaningful retries

- **Problem:** "Retry on error" either loops forever or repeats the same failure.
- **Solution:** Roles raise `TransientError` (retry with exponential backoff), `BlockedError` (needs a human),
  or `PermanentError` (fail now). The coding role widens its search tier on each attempt. Every attempt is
  persisted. Fault injection (`faults: {step: n}`) exists to demonstrate retries on demand and is labelled
  INJECTED in the log.
- **Reason / tradeoff:** Each failure maps to an explicit, honest outcome. Fault injection is a demo and test
  feature that ships in the product; it is opt-in per workflow.
- **Evidence:** `test_injected_transient_fault_is_retried`, `test_failed_attempt_is_retried_with_wider_search`.

## D8. Cooperative cancellation with process kill

- **Problem:** Cancel must work while a long test run or repair search is in progress, and must not leave
  half-applied mutations behind.
- **Solution:** A `cancel_requested` flag in the store. Roles call `check_stop()` between candidates, and the
  command runner polls it every 100 ms and kills the process tree. The repair loop restores the original file
  in a `finally` block.
- **Reason / tradeoff:** Cancellation is safe because it is cooperative, but latency depends on the polling
  interval (≈100 ms) rather than being instant.
- **Evidence:** `test_cancel_queued_and_running_workflows` (cancels mid-search, then asserts a clean worktree);
  also verified by hand in the dashboard.

## D9. Human approval is mandatory and is not a merge

- **Problem:** The brief requires waiting for a human and never auto-merging or deploying.
- **Solution:** Every pipeline ends in an `approval` step that sets `awaiting_approval`. Approve and reject
  require a named actor and are recorded in `approvals` and the audit log. No code path merges, pushes or deploys.
- **Reason / tradeoff:** It is slower than auto-merge by design. A future "auto-approve low risk" policy would
  be an explicit, separately approved change.
- **Evidence:** `test_bug_to_verified_fix_then_human_approval` (blank approver rejected; `main` unchanged after
  approval); `test_api_end_to_end`.

## D10. No-build dashboard served by FastAPI

- **Problem:** The dashboard must show real jobs, approvals, logs, tests and diffs. A React or Next.js toolchain
  adds hundreds of MB of `node_modules` on a nearly full disk before the backend is proven.
- **Solution:** One static HTML file with vanilla JS. It polls the JSON API every 1.5 s, re-renders only on
  change, and escapes all content. Every number comes from the store; LLM tokens and cost show 0 because they are 0.
- **Reason / tradeoff:** Zero build step and instant iteration, but less component structure. Moving to React
  later is straightforward because the API is the contract.
- **Evidence:** `test_api_end_to_end`; manual checks of submit → live progress → diff → approve → cancel in the browser.

## D11. No Redis

- **Problem:** Should a queue or broker be added for workers?
- **Solution:** No. Workers claim directly from the store with an atomic transaction and leases.
- **Reason / tradeoff:** It is one less service. Redis becomes justified only with many worker processes across machines.
- **Evidence:** `test_concurrent_workers_do_not_double_process`.

## D12. Deterministic triage, PRD, plan, review and support

- **Problem:** These roles need to produce useful structured output without an LLM.
- **Solution:** A keyword classifier with scores and confidence; a template PRD with acceptance criteria derived
  from the example; a planner that maps components to files; a review checklist computed from evidence; a
  support responder that retrieves from README sections. Each output names its method (for example,
  "rule-based keyword classifier v1").
- **Reason / tradeoff:** It is transparent and repeatable, but brittle on unusual wording. These are the first
  roles to hand to an LLM.
- **Evidence:** `test_support_question_gets_draft_answer_for_approval`, plus triage assertions in the workflow tests.
