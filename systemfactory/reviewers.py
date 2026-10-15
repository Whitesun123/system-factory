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
import warnings
from typing import TYPE_CHECKING, Callable, Mapping, Protocol

from .loop import parse_unified_diff

if TYPE_CHECKING:
    from .llm import Recorder


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


def _functions(source: str) -> list[tuple[str, int, int, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out: list[tuple[str, int, int, ast.FunctionDef | ast.AsyncFunctionDef]] = []
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
            for (_, old), (new_no, new) in zip(h["removed"], h["added"], strict=True):  # lengths checked above
                old_ops = [t.string for t in tokenize.generate_tokens(io.StringIO(old.strip() + "\n").readline)
                           if t.type == tokenize.OP]
                new_ops = [t.string for t in tokenize.generate_tokens(io.StringIO(new.strip() + "\n").readline)
                           if t.type == tokenize.OP]
                swaps = [(a, b) for a, b in zip(old_ops, new_ops, strict=True) if a != b] if len(old_ops) == len(new_ops) else []
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
        raise ReviewerError(f"reviewer reply was not valid JSON: {e}") from e
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


# ------------------------------------------------------------------ Ollama reviewers (real local inference)
OLLAMA_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        # property order matters for constrained decoding: the model writes its analysis before deciding
        "analysis": {"type": "string"},
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["blocking", "major", "minor"]},
            "title": {"type": "string"}, "detail": {"type": "string"},
            "file": {"type": "string"}, "function": {"type": "string"}, "line": {"type": "integer"},
            "evidence": {"type": "string"}, "suggestion": {"type": "string"}, "probe": {"type": "string"}},
            "required": ["severity", "title", "detail", "file", "function", "line", "evidence", "suggestion", "probe"]}},
        # specification-derived test cases; System Factory executes them, only executed failures block
        "checks": {"type": "array", "items": {"type": "object", "properties": {
            "function": {"type": "string"}, "call": {"type": "string"}, "expected": {"type": "string"},
            "rule": {"type": "string"}}, "required": ["function", "call", "expected", "rule"]}},
    },
    "required": ["analysis", "checks", "verdict", "summary", "findings"],
}

CHECKS_INSTRUCTIONS = """
Specification checks (required): for EVERY function listed under "Functions in the changed files", write
checks in `checks`, at least two per function: each is a call with literal arguments and the result the
documented rules (issue, acceptance criteria, docstrings, module docs) say it MUST return. Include the values
at, just below and just above every boundary the rules mention. `expected` is the exact Python repr of the
correct result (e.g. 0.0, 'some text', [1, 2]); derive it from the rules, not from what the code returns.
System Factory executes every check against the patched code; a failing check is a confirmed defect."""


def function_inventory(files_after: dict[str, str]) -> list[dict]:
    """Deterministic list of top-level functions (with signature lines) in the changed files."""
    out = []
    for path, src in files_after.items():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append({"file": path, "module": _module(path), "function": node.name,
                            "signature": lines[node.lineno - 1].strip()})
    return out


SAFE_BUILTINS = {"list", "tuple", "set", "dict", "range", "str", "int", "float", "bool", "sorted", "frozenset"}


def _safe_arg(node: ast.AST) -> bool:
    """Literals, or pure builtin constructors over literals (e.g. list(range(10))). No names or attributes."""
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError):
        pass
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in SAFE_BUILTINS:
        return all(_safe_arg(a) for a in node.args) and not node.keywords
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_safe_arg(e) for e in node.elts)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _safe_arg(node.operand)
    return False


_MISESCAPED = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # control characters other than \t and \n
_EXC_NAME = r"[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt)"
_EXC_TEXT = re.compile(rf"^(?:raises?\s+|an?\s+)?({_EXC_NAME})(?:\s*:\s*(.+))?$", re.S)


def expected_exception(expected: str) -> tuple[str, str | None] | None:
    """Recognise an expectation that a call RAISES: `ValueError('msg')`, `ValueError: msg`, `raises ValueError`
    or a pasted traceback. Returns (exception name, message or None), or None if the expectation is a value."""
    text = expected.strip()
    if text.startswith("Traceback"):
        text = text.splitlines()[-1].strip()
    try:
        node: ast.AST | None = ast.parse(text, mode="eval").body
    except SyntaxError:
        node = None
    if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)) and not node.keywords:
        name = ast.unparse(node.func)
        if not re.fullmatch(_EXC_NAME, name) or len(node.args) > 1:
            return None
        if not node.args:
            return name, None
        arg = node.args[0]
        return (name, arg.value) if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
    if node is not None and not isinstance(node, (ast.Name, ast.Attribute)):
        return None  # a literal value or another expression: not an exception
    m = _EXC_TEXT.match(text)
    if not m:
        return None
    msg = m.group(2).strip().strip("'\"") if m.group(2) else None
    return m.group(1), msg or None


def validate_check(check: dict, inventory: list[dict]) -> tuple[str | None, str | None]:
    """Turn a reviewer check into a doctest probe. Returns (probe, None) or (None, reason it is invalid)."""
    with warnings.catch_warnings():  # model text such as 'a\i' must not print SyntaxWarnings
        warnings.simplefilter("ignore", SyntaxWarning)
        return _validate_check(check, inventory)


def _validate_check(check: dict, inventory: list[dict]) -> tuple[str | None, str | None]:
    qualified = (check.get("function") or "").strip().removesuffix("()")
    name = qualified.split(".")[-1]
    module = qualified.rsplit(".", 1)[0] if "." in qualified else None
    matches = [f for f in inventory if f["function"] == name and (module is None or f["module"] == module)]
    if not matches:
        return None, f"function '{qualified}' is not a top-level function of the changed files"
    if len(matches) > 1:
        return None, (f"function name '{name}' is ambiguous ({', '.join(f['module'] for f in matches)}); "
                      "the check must qualify it with its module")
    fn = matches[0]
    try:
        expr = ast.parse(check.get("call", "").strip(), mode="eval").body
    except SyntaxError:
        return None, "call is not a Python expression"
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == name:
        expr.func = ast.Name(id=name, ctx=ast.Load())  # module.function(...) -> function(...)
    if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == name):
        return None, f"call is not a direct call of {name}()"
    if not all(_safe_arg(a) for a in [*expr.args, *(k.value for k in expr.keywords)]):
        return None, "arguments must be literals or pure builtin constructors"
    if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and _MISESCAPED.search(n.value)
           for n in ast.walk(expr)):
        # e.g. 'a\another' is BEL + 'nother', not a backslash: running it would test an input nobody intended
        return None, "a string argument contains a control character (probably an unescaped backslash)"
    expected = (check.get("expected") or "").strip()
    if not expected:
        return None, "no expected value"
    head = f">>> from {fn['module']} import {name}\n>>> {ast.unparse(expr)}"
    exc = expected_exception(expected)
    if exc:
        # An expected exception becomes a doctest traceback expectation, so a correct `raise` passes. The message
        # is reduced to one line, so it cannot start another `>>>` example.
        exc_name, msg = exc
        msg_line = " ".join((msg or "").split())
        if msg_line:
            return f"{head}\nTraceback (most recent call last):\n    ...\n{exc_name}: {msg_line}\n", None
        return (f"{head}  # doctest: +IGNORE_EXCEPTION_DETAIL\nTraceback (most recent call last):\n    ...\n"
                f"{exc_name}: ...\n"), None
    try:
        value = ast.literal_eval(expected)
    except (ValueError, SyntaxError):
        value = expected  # an unquoted text result: compare as a string
    # Always emit the single-line repr of the value: a multi-line literal could otherwise smuggle extra
    # `>>> ...` lines (i.e. arbitrary code) into the doctest.
    return f"{head}\n{repr(value)}\n", None


def check_to_probe(check: dict, inventory: list[dict]) -> str | None:
    return validate_check(check, inventory)[0]


from .loop import TRUSTED_REVIEWERS  # noqa: E402,F401  re-exported: deterministic reviewers written by System Factory
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "getattr", "setattr", "delattr", "globals", "locals", "vars",
                   "input", "breakpoint", "exit", "quit", "help", "memoryview", "classmethod", "staticmethod", "type"}
FORBIDDEN_NODES = (ast.Import, ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.With,
                   ast.AsyncWith, ast.Try, ast.While, ast.For, ast.AsyncFor, ast.Global, ast.Nonlocal, ast.Delete,
                   ast.Raise, ast.Await, ast.Yield, ast.YieldFrom)


def probe_is_safe(probe: str, allowed_modules: set[str]) -> bool:
    """Static vetting of a free-form doctest probe written by a model (or influenced by repository content).

    Allowed: `from <changed module> import ...`, assignments, calls and attribute access on ordinary names,
    literals. Rejected: other imports, dunder/underscore names or attributes, builtins that reach the
    filesystem or the interpreter, control flow and definitions. This is defence in depth on top of the
    process-level sandbox, not a substitute for a container.
    """
    try:
        examples = doctest.DocTestParser().get_examples(probe)
    except ValueError:
        return False
    if not examples:
        return False
    for ex in examples:
        try:
            tree = ast.parse(ex.source)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if isinstance(node, FORBIDDEN_NODES):
                return False
            if isinstance(node, ast.ImportFrom) and (node.level or node.module not in allowed_modules
                                                     or any(a.name == "*" or a.name.startswith("_") for a in node.names)):
                return False
            if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("_")):
                return False
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                return False
    return True


def vet_findings(findings: list[dict], files_after: dict[str, str]) -> list[dict]:
    """Drop unsafe probes from an untrusted reviewer's findings (the finding then counts as unverified)."""
    allowed = {_module(f) for f in files_after}
    out = []
    for f in findings:
        p = f.get("probe")
        if p and not probe_is_safe(p, allowed):
            f = {**f, "probe": None, "detail": (f.get("detail") or "") + " [probe rejected by safety vetting]"}
        out.append(f)
    return out

REVIEWER_FOCUS = {
    "ollama-a": ("Reviewer A", "requirements, correctness and architecture",
                 "Method: (1) In `analysis`, list every behavioural rule stated in the issue, the acceptance criteria, "
                 "and the module and function docstrings of the changed files. (2) For each rule, check EVERY function "
                 "in the changed files that implements or depends on it, including functions the patch did not touch, "
                 "and state whether it now behaves as the rule says. (3) Check the fix is made in the right place. "
                 "A reported problem that is fixed in one function but still present in another function implementing "
                 "the same rule is an incomplete fix."),
    "ollama-b": ("Reviewer B", "edge cases, regressions, security and maintainability",
                 "Method: (1) In `analysis`, identify the values the patch changes behaviour around (comparisons, "
                 "thresholds, ranges, special cases). (2) For every function in the changed files that depends on "
                 "those values, work out its result at, just below and just above each boundary, and compare with "
                 "the documented behaviour. (3) Check behaviour existing callers and tests rely on, unsafe constructs, "
                 "and whether the change is minimal and readable."),
}

OLLAMA_REVIEW_SYSTEM = """You are {label}, an independent code reviewer in an automated bug-fix pipeline.
Your focus: {focus}.
{instructions}
You review a patch to a Python repository. You cannot change code. Other reviewers work independently; you will
not see their output.
Report only problems you can justify from the material given. For every finding give: severity
(blocking = must be fixed before merge; major; minor), title, detail (explanation), file, function, line
(0 if unknown), evidence (what in the code or test output shows it) and suggestion (how to fix or validate).
For every BLOCKING finding about behaviour, `probe` MUST be a runnable doctest that FAILS on the patched code:
`>>> ` lines that import from the module and call it, each followed by the expected correct output, e.g.
">>> from pkg.mod import f\\n>>> f(3)\\n9". Probes are executed: a blocking finding whose probe does not fail
with a wrong result is downgraded. Use "" for probe on non-behavioural findings.
If the patch is correct and complete, return verdict "approve" with no blocking findings."""


class OllamaReviewer:
    """Independent reviewer backed by a local Ollama model. Real inference: simulated is False."""
    provider, simulated = "ollama", False

    def __init__(self, name: str, model: str, base_url: str | None = None, timeout: float | None = None):
        from .llm import OllamaClient
        self.name, self.model = name, model
        self.label, focus, self.instructions = REVIEWER_FOCUS[name]
        self.focus = focus
        self.technique = f"LLM review ({focus}) via local Ollama"
        self.client = OllamaClient(base_url=base_url, timeout=timeout)
        self.recorder: Recorder | None = None  # set by the review panel so every call becomes an llm_calls row

    def available(self) -> tuple[bool, str]:
        st = self.client.status([self.model])
        if not st["reachable"]:
            return False, st.get("error", "Ollama not reachable")
        if not st["models"].get(self.model, {}).get("installed"):
            return False, f"model {self.model} not installed (ollama pull {self.model})"
        return True, f"ollama {st['version']} at {self.client.base_url}"

    def review(self, bundle: dict, tools: Tools) -> dict:
        from .llm import ProviderError, parse_json
        system = (OLLAMA_REVIEW_SYSTEM.format(label=self.label, focus=self.focus, instructions=self.instructions)
                  + CHECKS_INSTRUCTIONS)
        inventory = function_inventory(bundle["files_after"])
        listing = "\n".join(f"- {f['file']}: `{f['signature']}`" for f in inventory) or "(none)"
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": render_bundle(bundle) +
                     f"\n\n# Functions in the changed files (write checks for each)\n{listing}"}]
        if self.recorder is None:
            raise ReviewerError("no call recorder attached: refusing an unrecorded model call")
        try:
            res, call = self.recorder.chat(self.client, self.model, messages, schema=OLLAMA_REVIEW_SCHEMA,
                                           purpose=f"{self.label} review")
        except ProviderError as e:
            raise ReviewerError(f"{type(e).__name__}: {e}") from e
        data = parse_json(res.content)
        if data.get("verdict") not in ("approve", "request_changes"):
            raise ReviewerError("reviewer reply did not match the review schema")
        findings = []
        for f in data.get("findings", []):
            findings.append({**f, "line": f.get("line") or None, "file": f.get("file") or None,
                             "function": f.get("function") or None, "probe": (f.get("probe") or "").strip() or None,
                             "rule": f"llm-{self.name}"})
        checks = []
        for c in data.get("checks", []):
            probe, problem = validate_check(c, inventory)
            name = (c.get("function") or "").removesuffix("()").split(".")[-1]
            checks.append({"function": name, "call": c.get("call"), "expected": c.get("expected"),
                           "rule": (c.get("rule") or "")[:300], "probe": probe, "valid": probe is not None,
                           "invalid_reason": problem,
                           "file": next((f["file"] for f in inventory if f["function"] == name), None)})
        return {"verdict": data["verdict"], "summary": data.get("summary", ""), "findings": findings,
                "checks": checks, "analysis": data.get("analysis", "")[:4000],
                "input_tokens": res.input_tokens, "output_tokens": res.output_tokens, "cost_usd": 0.0,
                "model": res.model, "llm_call_id": call["id"], "model_digest": call.get("model_digest"),
                "execution": "real-local"}


# ------------------------------------------------------------------ generic command reviewer
class CommandReviewer:
    """Any provider: a user-configured command reads the bundle JSON on stdin and prints a review JSON."""
    technique = "external reviewer process"

    def __init__(self, key: str, env: Mapping[str, str] | None = None):
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
        if not self.command:
            raise ReviewerError(f"{self.name}: no command configured")
        try:
            res = subprocess.run(self.command, shell=True, input=json.dumps(bundle), capture_output=True,
                                 text=True, encoding="utf-8", timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise ReviewerError(f"reviewer command timed out after {self.timeout}s") from e
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


def make(name: str, options: dict | None = None, settings=None) -> Reviewer:
    options = options or {}
    if name.startswith("command:"):
        return CommandReviewer(name.split(":", 1)[1])
    if name in REVIEWER_FOCUS:
        models = options.get("models", {})
        default = {"ollama-a": getattr(settings, "reviewer_a_model", "phi4:14b"),
                   "ollama-b": getattr(settings, "reviewer_b_model", "gemma3:12b")}[name]
        model = models.get("reviewer_a" if name == "ollama-a" else "reviewer_b") or default
        return OllamaReviewer(name, model, base_url=options.get("ollama_url"), timeout=options.get("llm_timeout"))
    if name not in FACTORIES:
        raise ReviewerError(f"unknown reviewer adapter {name!r}")
    return FACTORIES[name]()


def resolve(names: list[str], min_reviewers: int, options: dict | None = None, settings=None,
            allow_simulated: bool = True) -> tuple[list[Reviewer], list[str]]:
    """Instantiate available reviewers. With allow_simulated, top up with simulated ones (and say so) if too few
    are available; without it (real runtime) an unavailable reviewer is reported, never silently replaced."""
    chosen, notes = [], []
    for name in names:
        try:
            r = make(name, options, settings)
        except ReviewerError as e:
            notes.append(str(e))
            continue
        ok, why = r.available()
        if ok:
            chosen.append(r)
        else:
            notes.append(f"reviewer '{name}' unavailable: {why}")
    if not allow_simulated:
        return chosen, notes
    for name in FALLBACKS:
        if len(chosen) >= min_reviewers:
            break
        if name not in {r.name for r in chosen}:
            chosen.append(make(name))
            notes.append(f"substituted simulated reviewer '{name}' to reach {min_reviewers} independent reviewers")
    return chosen, notes


def diversity(reviews: list[dict]) -> dict:
    """Describe reviewer diversity truthfully. Different models on one runtime are NOT provider diversity."""
    ok = [r for r in reviews if r["verdict"] != "error"]
    real = [r for r in ok if not r["simulated"]]
    real_providers = sorted({r["provider"] for r in real})
    real_models = sorted({r["model"] for r in real})
    sim = len(ok) - len(real)
    if len(real_providers) >= 2:
        label = f"real multi-provider review ({', '.join(real_providers)})"
    elif real and len(real_models) >= 2:
        label = (f"REAL local inference: {len(real)} reviewers, {len(real_models)} different models "
                 f"({', '.join(real_models)}) on ONE provider ({real_providers[0]}); not multi-provider")
    elif real:
        label = f"REAL: {len(real)} reviewer(s), one model on one provider ({real_providers[0]})"
    else:
        label = "SIMULATED: no real models; reviewers are independent by technique, not by model diversity"
    if real and sim:
        label += f" + {sim} simulated"
    return {"reviewers": len(ok), "real_model_reviewers": len(real), "real_providers": real_providers,
            "real_models": real_models, "real_provider_diversity": len(real_providers) >= 2, "label": label}
