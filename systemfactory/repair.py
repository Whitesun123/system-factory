"""Deterministic search-based program repair (the *simulated* coding agent).

This is not an LLM. It is a small, honest automated-program-repair loop:
  1. Fault localisation: functions named in the issue's reproduction example (+ their callees).
  2. Candidate generation: single-token mutations (comparison/boolean/arithmetic operators,
     dropping an `± <number>` term, integer literal ±1), validated with ast.parse.
  3. Validation: a candidate is accepted only if the reproduction test passes AND the full
     regression suite passes.
Search breadth escalates per attempt (tier 1 -> 3), which is what makes retries meaningful.
"""
from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

COMPARE_SWAPS = {"<": ["<="], "<=": ["<"], ">": [">="], ">=": [">"], "==": ["!="], "!=": ["=="]}
ARITH_SWAPS = {"+": ["-"], "-": ["+"], "*": ["/"], "/": ["*"], "//": ["/"]}
BOOL_SWAPS = {"and": ["or"], "or": ["and"]}

TIERS: dict[int, dict[str, Any]] = {
    1: {"scope": "focus", "ops": ["compare", "bool"]},
    2: {"scope": "focus+callees", "ops": ["remove_term", "compare", "bool", "arith", "number"]},
    3: {"scope": "file", "ops": ["remove_term", "compare", "bool", "arith", "number"]},
}


@dataclass
class Candidate:
    file: str
    line: int
    op: str
    description: str
    new_text: str

    @property
    def key(self) -> str:
        return f"{self.file}:{self.line}:{self.description}"


@dataclass
class RepairResult:
    found: bool
    tier: int
    candidate: Candidate | None = None
    tried: list[dict] = field(default_factory=list)
    localisation: dict = field(default_factory=dict)


def identifiers_in_example(example_source: str) -> tuple[set[str], set[str]]:
    """Return (identifiers, imported modules) referenced by doctest-style `>>>` lines."""
    names: set[str] = set()
    modules: set[str] = set()
    for raw in example_source.splitlines():
        line = raw.strip()
        if not (line.startswith(">>>") or line.startswith("...")):
            continue
        code = line[3:].strip()
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
    return names, modules


@dataclass
class Function:
    name: str
    start: int
    end: int
    calls: set[str]


def functions_in(source: str) -> list[Function]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    if isinstance(sub.func, ast.Name):
                        calls.add(sub.func.id)
                    elif isinstance(sub.func, ast.Attribute):
                        calls.add(sub.func.attr)
            out.append(Function(node.name, node.lineno, node.end_lineno or node.lineno, calls))
    return sorted(out, key=lambda f: f.start)


def line_ranges(source: str, identifiers: set[str], scope: str) -> list[tuple[int, int]]:
    """Ordered line ranges to mutate, most suspicious first."""
    funcs = functions_in(source)
    focus = [f for f in funcs if f.name in identifiers]
    if scope == "focus":
        chosen = focus
    elif scope == "focus+callees":
        called = set().union(*(f.calls for f in focus)) if focus else set()
        chosen = focus + [f for f in funcs if f.name in called and f not in focus]
    else:
        n_lines = source.count("\n") + 1
        return [(f.start, f.end) for f in focus] + [(1, n_lines)]
    return [(f.start, f.end) for f in chosen]


def _replace(lines: list[str], row: int, c0: int, c1: int, new: str) -> str:
    line = lines[row - 1]
    out = lines[:]
    out[row - 1] = line[:c0] + new + line[c1:]
    return "".join(out)


def _valid(text: str) -> bool:
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def generate_candidates(file: str, source: str, ranges: list[tuple[int, int]],
                        ops: list[str]) -> Iterator[Candidate]:
    lines = source.splitlines(keepends=True)
    toks = [t for t in tokenize.generate_tokens(io.StringIO(source).readline)
            if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                              tokenize.INDENT, tokenize.DEDENT)]
    seen: set[str] = set()
    for lo, hi in ranges:
        for i, t in enumerate(toks):
            row, col = t.start
            if not (lo <= row <= hi) or t.end[0] != row:
                continue
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            for op in ops:
                muts: list[tuple[str, int, int, str]] = []  # description, c0, c1, replacement
                if op == "compare" and t.type == tokenize.OP and t.string in COMPARE_SWAPS:
                    muts = [(f"`{t.string}` -> `{r}`", col, t.end[1], r) for r in COMPARE_SWAPS[t.string]]
                elif op == "bool" and t.type == tokenize.NAME and t.string in BOOL_SWAPS:
                    muts = [(f"`{t.string}` -> `{r}`", col, t.end[1], r) for r in BOOL_SWAPS[t.string]]
                elif op == "arith" and t.type == tokenize.OP and t.string in ARITH_SWAPS:
                    muts = [(f"`{t.string}` -> `{r}`", col, t.end[1], r) for r in ARITH_SWAPS[t.string]]
                elif (op == "remove_term" and t.type == tokenize.OP and t.string in "+-"
                      and nxt is not None and nxt.type == tokenize.NUMBER and nxt.start[0] == row):
                    c0 = col
                    while c0 > 0 and lines[row - 1][c0 - 1] == " ":
                        c0 -= 1
                    muts = [(f"drop term `{t.string} {nxt.string}`", c0, nxt.end[1], "")]
                elif op == "number" and t.type == tokenize.NUMBER and t.string.isdigit():
                    v = int(t.string)
                    muts = [(f"literal `{v}` -> `{v + d}`", col, t.end[1], str(v + d)) for d in (1, -1) if v + d >= 0]
                for desc, c0, c1, rep in muts:
                    new = _replace(lines, row, c0, c1, rep)
                    k = f"{row}:{desc}:{c0}"
                    if new == source or k in seen or not _valid(new):
                        continue
                    seen.add(k)
                    yield Candidate(file, row, op, f"line {row}: {desc}", new)


def search(read: Callable[[str], str], write: Callable[[str, str], None], target_files: list[str],
           identifiers: set[str], tier: int, evaluate: Callable[[Candidate], dict],
           check_stop: Callable[[], None], max_evals: int,
           priority_lines: dict[str, list[int]] | None = None,
           skip: Callable[[Candidate], bool] | None = None) -> RepairResult:
    """Try candidates in order; leave the first fully validated one applied, restore all others.

    priority_lines: lines named in review feedback are mutated first.
    skip: predicate for candidates that must not be evaluated (e.g. a previously rejected solution).
    """
    spec = TIERS[min(tier, max(TIERS))]
    result = RepairResult(found=False, tier=tier, localisation={
        "scope": spec["scope"], "operators": spec["ops"], "identifiers": sorted(identifiers),
        "files": target_files, "skipped_rejected": 0})
    for file in target_files:
        original = read(file)
        ranges = line_ranges(original, identifiers, spec["scope"])
        ranges = [(ln, ln) for ln in (priority_lines or {}).get(file, [])] + ranges
        result.localisation.setdefault("ranges", {})[file] = ranges
        for cand in generate_candidates(file, original, ranges, spec["ops"]):
            check_stop()
            if skip is not None and skip(cand):
                result.localisation["skipped_rejected"] += 1
                continue
            if len(result.tried) >= max_evals:
                result.localisation["budget_exhausted"] = True
                return result
            write(file, cand.new_text)
            accepted = False
            try:
                verdict = evaluate(cand)
                result.tried.append({"candidate": cand.description, "file": file, **verdict})
                accepted = verdict.get("accepted", False)
                if accepted:
                    result.found, result.candidate = True, cand
                    return result
            finally:
                if not accepted:
                    write(file, original)
    return result
