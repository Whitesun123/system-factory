"""Real coding agent backed by a local Ollama model.

The model never gets a shell. Each turn it returns exactly one JSON action, validated against a schema:

    list_files                       -> tracked files in the worktree
    read_file {path}                 -> contents of a tracked, non-.git file (size-capped)
    run_tests                        -> fixed commands: reproduction test + full suite, in the sandbox
    submit {summary, edits[]}        -> search/replace edits, applied only to the plan's target files

Submitted edits must match the current file text exactly and uniquely, must keep the file valid Python,
and must not reproduce a previously rejected patch. After a submit the reproduction test and suite run
automatically; if they fail and turns remain, the result goes back to the model for another submit.
Every model call is an `llm_calls` row and every action is a `tool_calls` row.
"""
from __future__ import annotations

import ast
import json
import time

from . import loop
from .context import BlockedError, PermanentError, StepContext, TransientError
from .llm import InferenceTimeout, InvalidModelOutput, ModelNotFound, OllamaClient, ProviderError, \
    ProviderUnavailable, Recorder, parse_json

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["list_files", "read_file", "run_tests", "submit"]},
        "path": {"type": "string"},
        "summary": {"type": "string"},
        "edits": {"type": "array", "items": {
            "type": "object",
            "properties": {"file": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}},
            "required": ["file", "search", "replace"]}},
    },
    "required": ["action"],
}

SYSTEM = """You are the coding agent in System Factory, an automated bug-fix pipeline.
You work on a Python repository in an isolated git worktree. You cannot run shell commands.
Reply with exactly one JSON object per turn, choosing one action:
  {"action": "list_files"}
  {"action": "read_file", "path": "<repo-relative path>"}
  {"action": "run_tests"}   (runs the reproduction test and the full test suite on the current workspace)
  {"action": "submit", "summary": "<one sentence>", "edits": [{"file": "<path>", "search": "<exact current text>", "replace": "<new text>"}]}
Rules:
- You may edit ONLY these files: {targets}. Never edit tests.
- Make the smallest change that fully fixes the problem. Keep correct existing code unchanged.
- "search" must be copied exactly from the current file content (including indentation) and must occur exactly once.
- After you submit, the edits are applied and the tests run automatically; you will see the result.
"""


def _fence(path: str, text: str) -> str:
    return f"File `{path}`:\n```python\n{text}\n```"


def build_prompt(ctx: StepContext, n: int, prev: dict | None, fb: dict, strategy: str, probes: list[str],
                 rejected_diffs: list[tuple[int, str]], use_feedback: bool) -> str:
    wf = ctx.wf
    data = wf["data"]
    targets = data["plan"]["target_files"]
    ws = ctx.workspace([])
    test_rel = data["reproduce"]["test_path"]
    base_fail = next((t for t in ctx.store.get_test_runs(ctx.wf_id)
                      if t["step"] == "reproduce" and t["purpose"] == test_rel), None)
    parts = [f"# Issue #{wf['issue_id']}: {wf['issue_title']}\n\n{wf['issue_body']}",
             "# Acceptance criteria\n" + "\n".join(f"- {c}" for c in data["requirements"].get("acceptance_criteria", [])),
             "# Files you may edit (current workspace content)\n" + "\n\n".join(_fence(f, ws.read_text(f)) for f in targets),
             "# Reproduction test (must pass after your change; you may not edit it)\n" + _fence(test_rel, ws.read_text(test_rel))]
    if base_fail:
        parts.append(f"Its output on the original code (exit {base_fail['exit_code']}):\n```\n{base_fail['output_tail'][-1500:]}\n```")
    if prev is not None and use_feedback:
        parts.append(f"# This is iteration {n}. Revision of iteration {prev['n']}\n" + (
            "Your previous patch is ALREADY APPLIED in the files above; keep its correct parts and fix what the review found."
            if strategy == "build_on_previous" else
            "Your previous patch was DISCARDED because it broke tests or scope rules; the files above are the original code."))
        items = []
        for i in fb.get("items", []):
            loc = i["location"]
            items.append(f"- [{i['severity'].upper()}] {loc.get('file') or ''} {loc.get('where')}: {i['title']}\n" +
                         "".join(f"    - {f['reviewer']}: {f['detail'][:400]}\n" for f in i["findings"]) +
                         "".join(f"    - suggestion: {s[:300]}\n" for s in i["suggestions"]))
        parts.append("# Review feedback on your previous patch\n" + ("\n".join(items) or "(no findings)"))
        if probes:
            parts.append("# Checks that MUST pass after your change (doctest format: `>>>` lines, then expected output)\n" +
                         "\n".join(f"```\n{p.strip()}\n```" for p in probes))
        if rejected_diffs:
            parts.append("# Rejected patches: do NOT submit any of these again\n" +
                         "\n".join(f"Iteration {k}:\n```diff\n{d[-2500:]}\n```" for k, d in rejected_diffs))
    elif prev is not None:
        parts.append(f"# This is iteration {n}. (Feedback withheld: ablation mode `coder_feedback=false`.)")
    parts.append("Start by deciding whether you need more context; then submit your fix.")
    return "\n\n".join(parts)


def _apply_edits(current: dict[str, str], edits: list[dict], targets: list[str]) -> tuple[dict[str, str], list[str]]:
    new = dict(current)
    errors = []
    for k, e in enumerate(edits, 1):
        f = (e.get("file") or "").replace("\\", "/").lstrip("./")
        if f not in targets:
            errors.append(f"edit {k}: '{f}' is outside the allowed files {targets}")
            continue
        search, replace = e.get("search", ""), e.get("replace", "")
        count = new[f].count(search) if search else 0
        if count != 1:
            errors.append(f"edit {k}: search text occurs {count} times in {f} (must be exactly once); copy it exactly")
            continue
        new[f] = new[f].replace(search, replace, 1)
    for f in targets:
        if new[f] != current[f]:
            try:
                ast.parse(new[f])
            except SyntaxError as e:
                errors.append(f"{f} would not be valid Python after the edits: {e}")
    return new, errors


def run(ctx: StepContext, *, n: int, prev: dict | None, fb: dict, strategy: str, base: str, probes: list[str],
        rejected: set[str], checkpoint: str) -> dict:
    """One coding attempt with the Ollama model. Returns patch metadata after committing, or raises."""
    from .roles import commit_all  # local import: roles imports this module

    wf = ctx.wf
    data = wf["data"]
    targets = data["plan"]["target_files"]
    test_rel = data["reproduce"]["test_path"]
    model = wf["options"].get("models", {}).get("coder") or ctx.settings.coder_model
    use_feedback = wf["options"].get("coder_feedback", True)
    max_turns = int(wf["options"].get("coder_max_turns", ctx.settings.coder_max_turns))
    client = OllamaClient(base_url=wf["options"].get("ollama_url"), timeout=wf["options"].get("llm_timeout"))
    rec = Recorder(ctx.store, ctx.wf_id, n, ctx.step, "coding")
    ws = ctx.workspace(targets)
    read_only = ctx.workspace([])
    rejected_diffs = []
    if use_feedback:
        for it in ctx.store.get_iterations(ctx.wf_id):
            if it["n"] < n and it.get("commit") and it["fingerprint"] in rejected:
                rejected_diffs.append((it["n"], ctx.git.out(ctx.worktree, "diff", checkpoint, it["commit"], "--", *targets)))
    messages = [{"role": "system", "content": SYSTEM.replace("{targets}", ", ".join(targets))},
                {"role": "user", "content": build_prompt(ctx, n, prev, fb, strategy, probes, rejected_diffs, use_feedback)}]
    tracked = ctx.git.out(ctx.worktree, "ls-files").splitlines()
    accepted, summary, last_tests, turns = None, "", None, 0
    seed = 7 + ctx.attempt  # a retried attempt must not replay the identical deterministic conversation

    def tool(name: str, args: dict, ok: bool, result: str, t0: float, call_id: int | None):
        ctx.store.add_tool_call(ctx.wf_id, n, ctx.step, "coding", name, args, ok, result, round(time.time() - t0, 3), call_id)

    while turns < max_turns:
        ctx.check_stop()
        turns += 1
        try:
            res, call = rec.chat(client, model, messages, schema=ACTION_SCHEMA, purpose=f"coder turn {turns}",
                                 timeout=wf["options"].get("llm_timeout"), options={"seed": seed})
        except ModelNotFound as e:
            raise BlockedError(f"{e}. A human must install the model before the workflow can continue.")
        except (ProviderUnavailable, InferenceTimeout) as e:
            raise TransientError(f"coder model call failed: {e}")
        except ProviderError as e:
            raise TransientError(f"coder model call failed: {e}")
        ctx.charge(llm_tokens=res.input_tokens + res.output_tokens)
        messages.append({"role": "assistant", "content": res.content})
        t0 = time.time()
        try:
            act = parse_json(res.content)
        except InvalidModelOutput as e:
            tool("invalid", {}, False, str(e), t0, call["id"])
            messages.append({"role": "user", "content": f"Your reply was not valid JSON ({e}). Reply with one JSON action."})
            continue
        a = act.get("action")
        if a == "list_files":
            out = "\n".join(tracked)
            tool("list_files", {}, True, out, t0, call["id"])
            messages.append({"role": "user", "content": f"Tracked files:\n{out}"})
        elif a == "read_file":
            path = (act.get("path") or "").replace("\\", "/").lstrip("./")
            if path not in tracked:
                tool("read_file", {"path": path}, False, "not a tracked file", t0, call["id"])
                messages.append({"role": "user", "content": f"'{path}' is not a tracked file. Use list_files."})
                continue
            text = read_only.read_text(path)[:20000]
            tool("read_file", {"path": path}, True, f"{len(text)} chars", t0, call["id"])
            messages.append({"role": "user", "content": _fence(path, text)})
        elif a == "run_tests":
            r1, r2 = ctx.run_pytest(test_rel, purpose="coder self-check: repro"), ctx.run_pytest(purpose="coder self-check: suite")
            out = f"reproduction test: exit {r1.exit_code}\n{r1.output[-1200:]}\nfull suite: exit {r2.exit_code}\n{r2.output[-1200:]}"
            tool("run_tests", {}, r1.ok and r2.ok, out[-1500:], t0, call["id"])
            messages.append({"role": "user", "content": out})
        elif a == "submit":
            edits = act.get("edits") or []
            current = {f: read_only.read_text(f) for f in targets}
            new, errors = _apply_edits(current, edits, targets)
            if not edits:
                errors.append("submit contained no edits")
            if not errors and new == current:
                errors.append("the edits do not change any file")
            fp = loop.fingerprint(new)
            if not errors and fp in rejected and turns < max_turns:
                errors.append("this exact patch was already rejected in an earlier iteration; address the feedback "
                              "with a different change")
            if errors:
                tool("submit", {"edits": len(edits)}, False, "; ".join(errors), t0, call["id"])
                messages.append({"role": "user", "content": "Edits NOT applied:\n- " + "\n- ".join(errors) +
                                 "\nThe files are unchanged. Submit corrected edits."})
                continue
            for f in targets:
                if new[f] != current[f]:
                    ws.write_text(f, new[f])
            summary = (act.get("summary") or "").strip()[:300]
            r1 = ctx.run_pytest(test_rel, purpose="post-submit check: repro")
            r2 = ctx.run_pytest(purpose="post-submit check: suite")
            last_tests = {"repro": r1.exit_code, "suite": r2.exit_code}
            result = f"applied {len(edits)} edit(s); reproduction test exit {r1.exit_code}, suite exit {r2.exit_code}"
            tool("submit", {"edits": len(edits), "summary": summary, "fingerprint": fp}, True, result, t0, call["id"])
            if (r1.ok and r2.ok) or turns >= max_turns:
                accepted = fp
                break
            messages.append({"role": "user", "content":
                             f"Edits applied, but tests fail.\nreproduction test (exit {r1.exit_code}):\n{r1.output[-1200:]}\n"
                             f"full suite (exit {r2.exit_code}):\n{r2.output[-1200:]}\n"
                             "The files now contain your edits. Submit further edits to fix this."})
        else:
            tool("invalid", {"action": a}, False, "unknown action", t0, call["id"])
            messages.append({"role": "user", "content": f"Unknown action {a!r}. Use list_files, read_file, run_tests or submit."})

    if accepted is None:
        raise TransientError(f"coder ({model}) did not produce an applicable patch within {max_turns} turns")
    if ctx.git.out(ctx.worktree, "status", "--porcelain", "--", *targets) == "":
        raise TransientError("coder patch left the target files unchanged")
    changed = [f for f in targets if ctx.git.out(ctx.worktree, "status", "--porcelain", "--", f)]
    commit = commit_all(ctx, changed, f"fix: {wf['issue_title']} (issue #{wf['issue_id']}, iteration {n})\n\n"
                        f"Coding agent: ollama/{model}. {summary}" +
                        (f"\nAddresses review feedback: {', '.join(i['id'] for i in fb.get('items', []))}." if fb else ""))
    diff = ctx.git.out(ctx.worktree, "diff", f"{checkpoint}..HEAD", "--", *targets)
    patch = {"file": ", ".join(changed), "mutation": summary or "(no summary)", "tier": None, "coder": "ollama",
             "model": model, "strategy": strategy, "base": base,
             "addressed": [i["id"] for i in fb.get("items", [])] if use_feedback else [],
             "required_probes": len(probes) if use_feedback else 0, "turns": turns, "self_check": last_tests,
             "feedback_given": use_feedback and prev is not None, "skipped_rejected": 0}
    ctx.store.update_iteration(ctx.wf_id, n, base=base, commit=commit, fingerprint=accepted, patch=patch)
    ctx.evidence("patch", f"Iteration {n} patch by ollama/{model} (cumulative vs. repro commit)", diff, iteration=n,
                 fingerprint=accepted, model=model, turns=turns)
    ctx.log(f"Iteration {n}: ollama/{model} submitted a patch after {turns} turn(s): {summary}; committed {commit[:10]}")
    return {"agent": f"ollama/{model} (real local inference)", "iteration": n, **patch, "commit": commit,
            "fingerprint": accepted}
