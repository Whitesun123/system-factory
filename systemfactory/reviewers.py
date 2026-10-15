"""Independent reviewer adapters.

Every reviewer receives its own deep copy of the same review bundle (diff, requirements, test
evidence, previous iterations) and never sees another reviewer's output for the same iteration.

Adapters:
  simulated-spec   deterministic: executes the documented examples of every function the patch touches
  simulated-diff   deterministic: diff heuristics (sibling patterns, scope, size) + loop history
  claude           Claude via the official `anthropic` SDK; only when the SDK, ANTHROPIC_API_KEY and
                   SF_ALLOW_PAID_INFERENCE=1 are all present
  command:<NAME>   any other provider / local model: runs SF_REVIEWER_CMD_<NAME>, JSON bundle on stdin,
                   JSON review on stdout (see DECISIONS.md D14)

Each review records provider, model and a `simulated` flag. Two simulated reviewers are independent
by *technique*, not by model; nothing here reports simulated diversity as real model diversity.
"""
from __future__ import annotations

import ast
import doctest
import io
import json
import os
import re
import subprocess
import tokenize
from typing import Callable, Protocol

from .loop import parse_unified_diff


class ReviewerError(Exception):
    pass


class Tools(Protocol):
    def run_probe(self, source: str) -> tuple[bool, str]: ...


class Reviewer(Protocol):
    name: str
    provider: str
    model: str
    simulated: bool
    technique: str

    def available(self) -> tuple[bool, str]: ...
    def review(self, bundle: dict, tools: Tools) -> dict: ...


def _module(path: str) -> str:
    return path[:-3].replace("/", ".") if path.endswith(".py") else path


def _functions(source: str) -> list[tuple[str, int, int, ast.AST]]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node.lineno, node.end_lineno or node.lineno, node))
    return out


def _changed_new_lines(bundle: dict) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for h in parse_unified_diff(bundle["diff_u0"]):
        out.setdefault(h["file"], set()).update(n for n, _ in h["added"])
    return out


def _test_evidence_findings(tests: dict) -> list[dict]:
    out = []
    if not tests.get("repro_passed"):
        out.append({"severity": "blocking", "rule": "acceptance", "title": "Acceptance criterion not met: "
                    "reproduction test fails after the change", "evidence": tests.get("repro_summary", "")})
    if not tests.get("suite_passed"):
        out.append({"severity": "blocking", "rule": "acceptance", "title": "Acceptance criterion not met: "
                    "existing suite fails", "evidence": tests.get("suite_summary", "")})
    return out


# ------------------------------------------------------------------ simulated reviewer 1
class SpecConformanceReviewer:
    """Treats documented examples (docstring `>>>` blocks) of modified functions as part of the spec."""
    name, provider, model, simulated = "simulated-spec", "simulated", "spec-conformance-v1", True
    technique = "executes docstring examples of every function the patch modifies"

    def available(self) -> tuple[bool, str]:
        return True, "deterministic"

    def review(self, bundle: dict, tools: Tools) -> dict:
        findings = _test_evidence_findings(bundle["tests"])
        checked = 0
        for file, lines in _changed_new_lines(bundle).items():
            src = bundle["files_after"].get(file)
            if src is None:
                continue
            for name, start, end, node in _functions(src):
                if not any(start <= ln <= end for ln in lines):
                    continue
                doc = ast.get_docstring(node) or ""
                for ex in doctest.DocTestParser().get_examples(doc):
                    if not ex.want.strip() or "\n" in ex.source.strip():
                        continue
                    checked += 1
                    probe = (f">>> from {_module(file)} import {name}\n>>> {ex.source.strip()}\n"
                             f"{ex.want.strip()}\n")
                    ok, output = tools.run_probe(probe)
                    if ok:
                        continue
                    got = re.search(r"Got:\s*\n\s*(.+)", output)
                    findings.append({
                        "severity": "blocking", "rule": "documented-behaviour", "file": file,
                        "function": name, "line": start,
                        "title": f"Documented example fails for modified function `{name}`",
                        "detail": f"`{ex.source.strip()}` is documented to return `{ex.want.strip()}` but returns "
                                  f"`{got.group(1).strip() if got else '?'}` after the patch.",
                        "evidence": output[-1500:], "probe": probe,
                        "suggestion": f"Make `{name}` satisfy every documented example, not only the reported one."})
        verdict = "request_changes" if any(f["severity"] == "blocking" for f in findings) else "approve"
        return {"verdict": verdict, "findings": findings,
                "summary": f"Checked {checked} documented example(s) of modified functions; "
                           f"{len(findings)} finding(s)."}


# ------------------------------------------------------------------ simulated reviewer 2
class DiffRiskReviewer:
    """Static diff heuristics plus loop history; never executes code."""
    name, provider, model, simulated = "simulated-diff", "simulated", "diff-heuristics-v1", True
    technique = "diff heuristics: sibling patterns, scope, size, repeated solutions, unresolved feedback"

    def available(self) -> tuple[bool, str]:
        return True, "deterministic"

    def review(self, bundle: dict, tools: Tools) -> dict:
        tests = bundle["tests"]
        findings = _test_evidence_findings(tests)
        for f in tests.get("security", {}).get("findings", []):
            findings.append({"severity": "blocking" if f["severity"] == "high" else "major", "rule": f["rule"],
                             "title": f"Diff risk: {f['rule']}", "detail": f["detail"]})
        for p in tests.get("probes", []):
            if not p["passed"]:
                findings.append({"severity": "blocking", "rule": "unresolved-feedback", "probe": p["source"],
                                 "title": f"Feedback from an earlier iteration is still unresolved ({p['id']})",
                                 "evidence": p["output"][-800:]})
        for prev in bundle["previous_iterations"]:
            if prev.get("fingerprint") and prev["fingerprint"] == bundle.get("fingerprint"):
                findings.append({"severity": "blocking", "rule": "repeated-solution",
                                 "title": f"Patch is identical to the one rejected in iteration {prev['n']}",
                                 "suggestion": "Address the earlier feedback instead of resubmitting."})
        findings += self._sibling_patterns(bundle)
        hunks = parse_unified_diff(bundle["diff_u0"])
        changed = sum(len(h["added"]) + len(h["removed"]) for h in hunks)
        if changed > 10:
            findings.append({"severity": "major", "rule": "large-diff",
                             "title": f"Large change for a bug fix ({changed} changed lines)"})
        verdict = "request_changes" if any(f["severity"] == "blocking" for f in findings) else "approve"
        return {"verdict": verdict, "findings": findings,
                "summary": f"Reviewed {len(hunks)} hunk(s), {changed} changed line(s); {len(findings)} finding(s)."}

    @staticmethod
    def _sibling_patterns(bundle: dict) -> list[dict]:
        """If the patch changed `x > N` to `x >= N`, flag remaining `y > M` lines in the same function."""
        out = []
        changed = _changed_new_lines(bundle)
        for h in parse_unified_diff(bundle["diff_u0"]):
            src = bundle["files_after"].get(h["file"])
            if src is None or len(h["removed"]) != len(h["added"]):
                continue
            toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)]
            for (_, old), (new_no, new) in zip(h["removed"], h["added"]):
                old_ops = [t.string for t in tokenize.generate_tokens(io.StringIO(old.strip() + "\n").readline)
                           if t.type == tokenize.OP]
                new_ops = [t.string for t in tokenize.generate_tokens(io.StringIO(new.strip() + "\n").readline)
                           if t.type == tokenize.OP]
                swaps = [(a, b) for a, b in zip(old_ops, new_ops) if a != b] if len(old_ops) == len(new_ops) else []
                for name, start, end, _ in _functions(src):
                    if not start <= new_no <= end:
                        continue
                    for a, b in swaps:
                        for i, t in enumerate(toks[:-1]):
                            row = t.start[0]
                            if (t.type == tokenize.OP and t.string == a and start <= row <= end
                                    and row not in changed.get(h["file"], set())
                                    and toks[i + 1].type == tokenize.NUMBER):
                                line_text = src.splitlines()[row - 1].strip()
                                out.append({
                                    "severity": "major", "rule": "inconsistent-boundary", "file": h["file"],
                                    "function": name, "line": row,
                                    "title": f"Same `{a}` boundary pattern left unchanged at line {row}",
                                    "detail": f"The patch changed `{a}` to `{b}` at line {new_no} but `{line_text}` "
                                              f"in the same function still uses `{a}`.",
                                    "suggestion": f"Check whether line {row} needs the same `{a}` -> `{b}` change."})
        return out


# ------------------------------------------------------------------ Claude reviewer (official SDK)
CLAUDE_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output), from the Claude API pricing table.
CLAUDE_PRICING = {"claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
                  "claude-sonnet-5-5": (2.0, 10.0), "claude-fable-5-1": (10.0, 50.0)}

REVIEW_SYSTEM = (
    "You are an independent senior code reviewer in an automated bug-fix pipeline. Review the patch against "
    "the issue, the requirements and the test evidence. Report only defects you can justify from the material "
    "given. For each behavioural defect, include an executable doctest probe (`>>> ` lines plus expected "
    "output) that fails on the patched code; probes are re-executed and unconfirmed findings are downgraded. "
    "Reply with a single JSON object and nothing else: "
    '{"verdict": "approve"|"request_changes", "summary": str, "findings": [{"severity": '
    '"blocking"|"major"|"minor", "rule": str, "title": str, "detail": str, "file": str|null, '
    '"function": str|null, "line": int|null, "suggestion": str, "probe": str|null}]}. '
    "Use severity blocking only for defects that must be fixed before merge."
)


def render_bundle(bundle: dict) -> str:
    parts = [f"# Issue\n{bundle['issue']['title']}\n\n{bundle['issue']['body']}",
             "# Requirements (PRD)\n" + json.dumps(bundle["prd"], indent=1),
             "# Plan\n" + json.dumps({k: bundle["plan"].get(k) for k in ("target_files", "steps")}, indent=1),
             f"# Patch (iteration {bundle['iteration']}, against the reproduction-test commit)\n```diff\n"
             f"{bundle['diff']}\n```",
             "# Deterministic test evidence\n" + json.dumps(
                 {k: bundle["tests"].get(k) for k in ("repro_passed", "suite_passed", "repro_summary",
                                                      "suite_summary", "probes_passed")}, indent=1),
             "# Previous iterations\n" + json.dumps(bundle["previous_iterations"], indent=1)]
    for f, src in bundle["files_after"].items():
        parts.append(f"# {f} after the patch\n```python\n{src}\n```")
    return "\n\n".join(parts)


def parse_review_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ReviewerError("reviewer reply contained no JSON object")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ReviewerError(f"reviewer reply was not valid JSON: {e}")
    if data.get("verdict") not in ("approve", "request_changes") or not isinstance(data.get("findings", []), list):
        raise ReviewerError("reviewer reply did not match the review schema")
    return data


class ClaudeReviewer:
    name, provider, technique = "claude", "anthropic", "LLM review of diff, requirements and test evidence"

    def __init__(self, client=None, model: str = CLAUDE_MODEL, label_simulated: bool = False):
        self._client = client
        self.model = model
        self.simulated = label_simulated  # True only for test doubles: never claim a fake is a real model

    def available(self) -> tuple[bool, str]:
        if self._client is not None:
            return True, "client injected"
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY not set"
        if os.environ.get("SF_ALLOW_PAID_INFERENCE") != "1":
            return False, "paid inference not approved (set SF_ALLOW_PAID_INFERENCE=1)"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic SDK not installed (pip install anthropic)"
        return True, "configured"

    def review(self, bundle: dict, tools: Tools) -> dict:
        client = self._client
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # server-side refusal fallback, routed by refusal category
            output_config={"effort": "high"},
            system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": render_bundle(bundle)}],
        )
        served = getattr(response, "model", self.model)
        in_tok = response.usage.input_tokens
        out_tok = response.usage.output_tokens
        p_in, p_out = CLAUDE_PRICING.get(served, CLAUDE_PRICING[CLAUDE_MODEL])
        cost = (in_tok * p_in + out_tok * p_out) / 1_000_000
        if response.stop_reason == "refusal":
            raise ReviewerError(f"model declined to review (cost ${cost:.4f})")
        text = "".join(b.text for b in response.content if b.type == "text")
        data = parse_review_json(text)
        return {"verdict": data["verdict"], "summary": data.get("summary", ""), "findings": data.get("findings", []),
                "input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": cost, "model": served}


# ------------------------------------------------------------------ generic command reviewer
class CommandReviewer:
    """Any provider: a user-configured command reads the bundle JSON on stdin and prints a review JSON."""
    technique = "external reviewer process"

    def __init__(self, key: str, env: dict | None = None):
        env = env if env is not None else os.environ
        p = f"SF_REVIEWER_CMD_{key.upper()}"
        self.name = f"command:{key}"
        self.command = env.get(p)
        self.provider = env.get(f"{p}_PROVIDER", "unknown")
        self.model = env.get(f"{p}_MODEL", "unknown")
        self.simulated = env.get(f"{p}_SIMULATED", "1") != "0"  # assume simulated unless declared real
        self.timeout = float(env.get(f"{p}_TIMEOUT", "120"))

    def available(self) -> tuple[bool, str]:
        return (True, "configured") if self.command else (False, f"SF_REVIEWER_CMD_{self.name[8:].upper()} not set")

    def review(self, bundle: dict, tools: Tools) -> dict:
        try:
            res = subprocess.run(self.command, shell=True, input=json.dumps(bundle), capture_output=True,
                                 text=True, encoding="utf-8", timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise ReviewerError(f"reviewer command timed out after {self.timeout}s")
        if res.returncode != 0:
            raise ReviewerError(f"reviewer command exited {res.returncode}: {res.stderr.strip()[-300:]}")
        data = parse_review_json(res.stdout)
        return {"verdict": data["verdict"], "summary": data.get("summary", ""), "findings": data.get("findings", []),
                "input_tokens": int(data.get("input_tokens", 0)), "output_tokens": int(data.get("output_tokens", 0)),
                "cost_usd": float(data.get("cost_usd", 0.0))}


# ------------------------------------------------------------------ registry
FACTORIES: dict[str, Callable[[], Reviewer]] = {
    "simulated-spec": SpecConformanceReviewer,
    "simulated-diff": DiffRiskReviewer,
    "claude": ClaudeReviewer,
}
FALLBACKS = ["simulated-spec", "simulated-diff"]


def make(name: str) -> Reviewer:
    if name.startswith("command:"):
        return CommandReviewer(name.split(":", 1)[1])
    if name not in FACTORIES:
        raise ReviewerError(f"unknown reviewer adapter {name!r}")
    return FACTORIES[name]()


def resolve(names: list[str], min_reviewers: int) -> tuple[list[Reviewer], list[str]]:
    """Instantiate available reviewers; top up with simulated ones (and say so) if too few are available."""
    chosen, notes = [], []
    for name in names:
        try:
            r = make(name)
        except ReviewerError as e:
            notes.append(str(e))
            continue
        ok, why = r.available()
        if ok:
            chosen.append(r)
        else:
            notes.append(f"reviewer '{name}' unavailable: {why}")
    for name in FALLBACKS:
        if len(chosen) >= min_reviewers:
            break
        if name not in {r.name for r in chosen}:
            chosen.append(make(name))
            notes.append(f"substituted simulated reviewer '{name}' to reach {min_reviewers} independent reviewers")
    return chosen, notes


def diversity(reviews: list[dict]) -> dict:
    ok = [r for r in reviews if r["verdict"] != "error"]
    real = [r for r in ok if not r["simulated"]]
    real_providers = sorted({r["provider"] for r in real})
    if len(real_providers) >= 2:
        label = f"real multi-provider review ({', '.join(real_providers)})"
    elif real:
        label = f"{len(real)} real model reviewer(s) from one provider + {len(ok) - len(real)} simulated"
    else:
        label = "SIMULATED: no real models; reviewers are independent by technique, not by model diversity"
    return {"reviewers": len(ok), "real_model_reviewers": len(real), "real_providers": real_providers,
            "real_provider_diversity": len(real_providers) >= 2, "label": label}
