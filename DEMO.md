# 5-Minute Demo

## Before the demo (1 minute, off-screen)

```bash
python -m systemfactory init-demo
```

```bash
python -m systemfactory serve
```

Open http://127.0.0.1:8765. To start from an empty board, stop the server, delete the `data/` folder
(it holds only generated state), and start again.

## The planted bugs (in `demo/shopcart_template`)

| Sample | Bug | Expected outcome |
|---|---|---|
| `shipping` | `subtotal() > 50` should be `>=` | Fixed on attempt 1 (tier 1: comparison operators) |
| `pagination` | `items[start:start + page_size - 1]` | Attempt 1 fails, retry fixes it on attempt 2 (tier 2: drop `- 1`) |
| `coupon` | Codes should be case-insensitive (needs `.upper()`) | Beyond single-token repair: 3 attempts, then **failed** with evidence |
| `not_reproducible` | Report is wrong; the example already passes | **Blocked**: "could not reproduce" |
| `feature` | CSV export request | **Blocked** at coding: needs an LLM or a human developer |
| `support` | "How do I apply a coupon?" | Draft answer from README, waiting for approval |

## Script

**0:00 to 0:30: Frame it.** Point at the header: *"Runtime: SIMULATED (no LLM)."* Say: "The control
plane is real and deterministic. The coding agent is a search-based repair engine, not an LLM. I'll show you
exactly where an LLM would plug in."

**0:30 to 2:00: Happy path (shipping).** Submit the "free shipping at exactly $50" sample. While it runs:
- Point out each pipeline chip turning green (triage → … → review), then **approval: waiting**.
- **Evidence** tab: open "Reproduction test on base commit (expected to FAIL)" and show the failing doctest.
  Then show "after fix (expected to PASS)" and the full suite passing.
- **Diff** tab: one line changes, `>` → `>=`, plus the generated test file.
- Say: "Main is untouched. This lives on branch `sf/wf-N` in its own worktree."
- Type a name and click **Approve**. The banner says the branch is ready for a human to merge. "It never merges itself."

**2:00 to 3:00: Error and retry (pagination).** Submit the pagination sample.
- **Attempts** tab: coding attempt 1 **failed**, "Tier 1 search … evaluated 3 candidates." Attempt 2 **succeeded**.
- Say: "Retries widen the search. A retry isn't a blind repeat." Show the diff: `- 1` removed.
- Optional: submit `shipping` with **Inject a transient fault in reproduce** ticked. The log shows
  `INJECTED transient fault`, backoff, then success.

**3:00 to 3:45: Cancellation.** Select `coupon`, tick **Slow down repair**, and submit. When the coding chip shows
attempt 3, click **Cancel**. The state becomes `cancelled` within about a second, and the coding attempt is
marked `cancelled` in the Attempts tab. Say: "The running test process was killed, and the half-applied
mutation was rolled back. The worktree is clean."

**3:45 to 4:30: Honest limits.** Submit `coupon` without the slow-down option. It fails after 3 tiers, and the
banner says "retries exhausted." Submit `feature`: it is **blocked**, with the message that an LLM coding
runtime is not configured. Say: "When the system can't do something, it says so and hands off. It does not
pretend."

**4:30 to 5:00: Wrap.** Show the **Log** tab, where every transition has an actor and a timestamp. Then
show the **Data** tab, with the structured PRD, plan, test results and review checklist. Close with: "Next
step: a Claude provider behind the same role interface, running in containers, with real token budgets."

## CLI-only fallback

```bash
python -m systemfactory demo
```

```bash
python -m systemfactory show 2
```

```bash
python -m systemfactory approve 1 --by "Your Name" --comment "verified"
```

## Evidence this demo works

`python -m pytest -q` passes 22 tests. They include the same scenarios end to end, with real git worktrees
and real pytest runs: `tests/test_workflows.py`.
