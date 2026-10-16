# 5-Minute Demo

Two parts. **Part 1** uses the deterministic runtime: no GPU or network needed, every result reproducible, and the
same scenarios run in CI. Use it for the live demo. **Part 2** is optional and shows the same pipeline with real
local models via Ollama. Present it as "real local inference on two small synthetic bugs, repeated at fixed seeds" (table below).

## Before the demo (1 minute, off-screen)

```bash
python -m systemfactory init-demo
```

```bash
python -m systemfactory serve
```

Open http://127.0.0.1:8765. To start from an empty board, stop the server, move or delete the `data/` folder
(it holds only generated state), and start again. **After pulling a new version of the demo template, do this
too** (or run `init-demo --force`), because an existing demo repo is never overwritten.

## The planted bugs (in `demo/shopcart_template`)

| Sample | Bug | Expected outcome |
|---|---|---|
| `bulk` | `quantity > 10` **and** `quantity > 50` should both be `>=`; the issue reports only the first | **Fails review in iteration 1, corrected in iteration 2**, quality gate SUCCESS |
| `bulk` + stubborn coder | same, but the coder is a test double that ignores feedback | **Blocked** in iteration 2: "Repeated solution", escalated |
| `shipping` | `subtotal() > 50` should be `>=` | Passes review in iteration 1 |
| `pagination` | `items[start:start + page_size - 1]` | Coding attempt 1 fails, the retry fixes it on attempt 2 (tier 2: drop `- 1`) |
| `coupon` | Codes should be case-insensitive (needs `.upper()`) | Beyond single-token repair: 3 attempts, then **failed** with evidence |
| `not_reproducible` | Report is wrong; the example already passes | **Blocked**: "could not reproduce" |
| `feature` | CSV export request | **Blocked** at coding: needs an LLM or a human developer |
| `support` | "How do I apply a coupon?" | Draft answer from README, waiting for approval |

## Part 1: deterministic runtime (recommended for the live demo)

In the dashboard, set **Runtime** to **Deterministic simulation** before submitting.

**0:00 to 0:30: Frame it.** Point at the header badge and the **Runtime & providers** panel. Say: "The control
plane is deterministic in both runtimes. Here the coder and both reviewers are deterministic simulations, and every
screen says so. The same pipeline runs real local models; I'll show what has been verified there at the end."

**0:30 to 2:15: The review loop (bulk).** Submit "bulk discount at exactly 10 units". Open the **iterations** tab.
- **Iteration 1:** the coder changes `> 10` to `>= 10`, and the deterministic tests are all green. Say: "Tests alone
  would ship this."
- The two reviewers got the same diff, requirements and test evidence, and neither saw the other's output.
  `simulated-spec` executes the function's documented examples and **blocks**: `bulk_discount_percent(50)` returns
  5.0, documented 10.0. `simulated-diff` independently notes the same `>` pattern left on line 57.
- **Aggregated feedback F1:** blocking, **corroborated by both reviewers**, "confirmed by re-execution", with a
  suggestion and a **required probe**. The decision is **revise**, and the next revision builds on the previous patch.
- **Iteration 2:** "addresses F1 with 1 required probe". The coder fixes line 57, the tests and the probe pass, and
  both reviewers approve.
- **Quality gate SUCCESS:** every PRD acceptance criterion is ticked off against a re-run check. Type a name and
  **Approve**. Say: "Approval is a recorded decision, not a merge."

**2:15 to 3:00: Stall detection.** Tick **stubborn coder**, then submit `bulk` again. Iteration 2 resubmits the same
patch, and the workflow is **blocked** with "Repeated solution … same fingerprint". The red banner lists the open
blocking item and the recommended action. Say: "It escalates instead of looping. The iteration count, loop time,
tokens and cost are all budgeted."

**3:00 to 3:45: Retries and cancellation.** Submit `pagination`. The **Attempts** tab shows coding attempt 1 failed in
iteration 1 and attempt 2 succeeded. Say: "A transient retry widens the search inside an iteration; a revision is
driven by review across iterations." Then submit `coupon` with **Slow down repair** ticked and click **Cancel**
during coding. The workflow is cancelled, the iteration is marked cancelled, and the worktree is clean.

**3:45 to 4:30: Honest limits.** `feature` is **blocked** at coding: neither runtime implements feature work yet.
The badge reads "SIMULATED: no real models; reviewers are independent by technique". Say: "With Ollama the
reviewers are real models, but two models on one runtime are still one provider, and the label says so."

**4:30 to 5:00: Wrap.** Show the **Log** tab, where every phase transition (`Iteration 1: reviewing -> aggregating`) and
every decision has an actor and a timestamp. Click **Export evidence (.zip)**. Close with: "The same pipeline
runs real local models in a Docker sandbox; here are the recorded real runs."

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

## Part 2: real local inference with Ollama (optional)

Prerequisites: Ollama running and the models pulled (README > Configure Ollama). Then `python scripts\check_models.py`
must report all three models as installed. That script only lists models; it does not run inference.

Pick **Real local inference (Ollama)** in the dashboard and submit `checkout` or `bulk`. Show the **agents** tab: every
model call with model and digest, the tokens reported by Ollama, timing, the coder's tool calls (`run_tests`,
`submit`), and each reviewer's executed checks. A cold model load takes 7–25 s on an RTX 5080.

| Real-model scenario | Result (2 runs x repeat 2, temperature 0, fixed seeds; `examples/evidence/real/`) |
|---|---|
| A. Genuine bug fixed by the real coder, approved by real reviewers | 4/4 pass in 1 iteration, gate SUCCESS, hidden oracle satisfied |
| B. Incomplete first fix, real review feedback, revision, gate | 4/4 revise then pass: both reviewers confirmed `shipping_banner(50.0)` by execution; iteration 2 fixed it |
| C. Repeated solution / stall with a real coder (feedback ablation) | 2/2 stalled on the same fingerprint, escalated |
| D. Provider unavailable / model timeout / cancellation | 2/2 each: failed after 3 attempts with recorded failed calls / failed / cancelled |
| E. Approval guard on real-runtime workflows | only gate-SUCCESS workflows approvable; demo `main` unchanged |

To run all five: `python scripts\real_demo.py --repeat 2`. It needs Docker and the sandbox image (the real runtime
refuses to run without isolation), writes evidence to `data-real-demo/evidence/`, and checks results against an
oracle the agents never see. If a model fails, the run shows the real failure; it never substitutes a simulation.

## Evidence this demo works

- `python -m pytest -q` runs the deterministic suite, including Part 1's scenarios end to end with real git worktrees and
  real pytest runs (`tests/test_workflows.py`, `tests/test_review_loop.py`). It runs on Windows and Ubuntu in GitHub
  Actions.
- Sanitized evidence from deterministic runs of Part 1: `examples/evidence/case-a-revision-after-review/` and
  `examples/evidence/case-b-repeated-solution-escalated/`. Screenshots: `docs/screenshots/`.
