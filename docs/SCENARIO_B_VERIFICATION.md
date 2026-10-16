# Scenario B verification: review-driven revision with real models

Checked directly against the saved evidence package
[`examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/),
not against the README. Run on System Factory revision `7f6a5b2` (clean checkout), one provider (local Ollama).
The same outcome is recorded for B2 in this run and for B1/B2 in
[`run-1-c8026e0`](../examples/evidence/real/run-1-c8026e0/).

**Issue (sample `checkout`):** "Customers with exactly $50.00 are charged shipping". The report names only
`shipping_cost`; `shipping_banner` has the same `<` vs `<=` boundary defect and is not mentioned.

| # | Claim | Evidence in the package |
|---|---|---|
| 1 | Run configuration was real and isolated | [`run-manifest.json`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/run-manifest.json): `runtime: ollama`, `execution: docker`, `require_isolation: true`, `allow_simulated_reviewers: false`; models `qwen2.5-coder:14b` (digest `9ec8897f747e`), `phi4:14b` (`ac896e5b8b34`), `qwen3:14b` (`bdbd181c33f2`); `source_revision 7f6a5b2…`, `source_dirty: false` |
| 2 | The first implementation was incomplete | [`llm-calls.json`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/llm-calls.json) call 10 (iteration 1 `submit`): one edit, `if subtotal > FREE_SHIPPING_THRESHOLD:` → `>=`, in `shipping_cost` only |
| 3 | The deterministic tests passed on that incomplete patch | [`test-results.json`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/test-results.json): iteration 1 `testing` step, reproduction test exit 0 and suite exit 0 (18 passed) on commit `f7ae7e42` |
| 4 | Both reviewers **approved** iteration 1 by verdict | [`review-findings.json`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/review-findings.json): iteration 1, `ollama-a` (phi4) `approve`, `ollama-b` (qwen3) `approve` |
| 5 | Their own executable checks exposed the remaining defect | [`iterations.json`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/iterations.json), iteration 1 `feedback.checks`: both reviewers proposed `shipping_banner(50.0)` → `'Your order ships free!'`; executed result `'Add $0.00 more for free shipping'` (`outcome: mismatch`) |
| 6 | The finding was grounded and corroborated | same record: `evidence_state: confirmed`, `grounding: 'Your order ships free!'` (the documented banner text), `corroborated_by` the other reviewer. Reviewer B's wrong check `shipping_banner(49.99)` → `'Add $5.01 …'` was classified `conflicting` and did **not** block |
| 7 | Checks ran in the Docker sandbox | Every test run in `test-results.json` (14 of 14) records `docker (container)`. Reviewer checks go through the same single execution choke point (`StepContext.run_python`) and the workflow's backend (`execution: docker`, isolation required). *The package does not record a backend field per individual check execution.* |
| 8 | The coding agent received specific feedback | `llm-calls.json`, first iteration-2 coder prompt: contains `shipping_banner(50.0)`, the expected `Your order ships free!` and the observed `Add $0.00`. `iterations.json` iteration 2 patch: `strategy: build_on_previous`, `addressed: [F1, F2]`, `required_probes: 2` |
| 9 | Iteration 2 made a meaningful change | `llm-calls.json` call 14 (iteration 2 `submit`): `if remaining < 0:` → `if remaining <= 0:` in `shipping_banner`. Final diff: [`patch.diff`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/patch.diff) (both hunks plus the generated reproduction test) |
| 10 | Required checks and the full suite passed | `iterations.json` iteration 2 tests: reproduction and suite passed, required probes `[True, True]`; `test-results.json` iteration 2 testing exit 0 on commit `3ab50d83` |
| 11 | The quality gate succeeded on the final commit | [`quality-gate.json`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/quality-gate.json): `SUCCESS` on `3ab50d83…`; reproduction fails on base, passes after; suite 18 passed; 2/2 review probes pass; scope and security pass |
| 12 | An independent oracle accepted the result | [`scenario-results.json`](../examples/evidence/real/run-2-7f6a5b2/scenario-results.json): `spec_satisfied_by_oracle: true`. The oracle (`ORACLES["checkout"]` in `scripts/real_demo.py`) checks `shipping_cost` at 50.0 and 49.99 and `shipping_banner` at 50.0 and 45.0, and is never shown to the agents |
| 13 | Human approval did not merge or push | [`events.json`](../examples/evidence/real/run-2-7f6a5b2/B1-revision-after-review-wf3/events.json): `awaiting_approval → approved` by `human:demo-reviewer`, then "Branch sf/wf-3 is ready for a human to merge. System Factory does not merge, push or deploy." `scenario-results.json` (scenario E): `demo_main_unchanged: true` |

## What this does and does not show

- It shows the full loop with real models on one small synthetic bug: real coder → deterministic tests (green) →
  two real reviews → executed checks → grounded, corroborated finding → feedback → real revision → gate → human
  approval. It is repeated 4 times across two runs at temperature 0 with fixed seeds.
- **The reviewers' verdicts were wrong in iteration 1** (both `approve` on an incomplete fix). The system did not act
  on the verdicts; it acted on their executed checks. This is the central design point (DECISIONS.md D18, D22).
- It is not evidence of reliability on unseen real-world issues, and it is not an automation rate.
