"""Pick the code a verifier needs: the enclosing function, its diff hunks, callees, and callers.

Regex and indentation heuristics only. Reads git objects; never runs repository code.
"""

from __future__ import annotations

import re
from pathlib import Path

from pairrot.models import _SYMBOL_PATTERNS
from pairrot.snapshot import run_git

FUNCTION_LINES = 80
CALLEE_LINES = 40
CALLER_CONTEXT = 3
MAX_CALLEES = 3
MAX_CLASSES = 2
MAX_CALLERS = 4

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_CALL = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
    r"|\bexcept\s+\(?\s*([A-Za-z_][A-Za-z0-9_]*)"
    r"|\bcatch\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)"
)
_NOT_CALLEES = frozenset(
    "if for while return print len int str float bool dict list set tuple range "
    "isinstance super type max min sum any all sorted enumerate zip map filter open".split()
)


def _defined_name(line: str) -> str | None:
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def block_end(lines: list[str], start: int, cap: int = FUNCTION_LINES * 3) -> int:
    """Last 1-based line of the definition that starts at 1-based line start."""
    head = lines[start - 1]
    limit = min(len(lines), start + cap)
    if "{" in head and not head.rstrip().endswith(":"):
        depth = 0
        for number in range(start, limit + 1):
            depth += lines[number - 1].count("{") - lines[number - 1].count("}")
            if depth <= 0 and number > start:
                return number
        return limit
    base = _indent(head)
    last = start
    for number in range(start + 1, limit + 1):
        text = lines[number - 1]
        if not text.strip():
            continue
        if _indent(text) <= base:
            break
        last = number
    return last


def enclosing(lines: list[str], start: int, end: int) -> tuple[int, int] | None:
    """1-based range of the innermost definition that contains start..end."""
    for number in range(min(start, len(lines)), 0, -1):
        if _defined_name(lines[number - 1]) is None:
            continue
        last = block_end(lines, number)
        if last >= end:
            return number, last
    return None


def enclosing_name(lines: list[str], start: int, end: int) -> str | None:
    span = enclosing(lines, start, end)
    return _defined_name(lines[span[0] - 1]) if span else None


def numbered(lines: list[str], first: int, last: int) -> str:
    return "\n".join(f"{n}:{lines[n - 1]}" for n in range(first, min(last, len(lines)) + 1))


def function_window(lines: list[str], start: int, end: int) -> tuple[int, int, bool]:
    """Function around the finding, or a window inside it when the function is long."""
    span = enclosing(lines, start, end)
    if span is None:
        return max(1, start - 15), min(len(lines), end + 15), False
    first, last = span
    if last - first + 1 <= FUNCTION_LINES:
        return first, last, True
    half = FUNCTION_LINES // 2
    return max(first, start - half), min(last, end + half), False


def diff_hunks(diff: str, first: int, last: int, margin: int = 3) -> str:
    """Diff rows whose head-side line falls in first..last (plus margin). Whole diff if none do."""
    lo, hi = first - margin, last + margin
    out: list[str] = []
    head_line: int | None = None
    run_open = False
    for row in diff.splitlines():
        match = _HUNK.match(row)
        if match:
            head_line = int(match.group(1))
            run_open = False
            continue
        if head_line is None or row.startswith("\\"):
            continue
        inside = lo <= head_line <= hi
        if inside:
            if not run_open:
                out.append(f"@@ head {head_line} @@")
                run_open = True
            out.append(row)
        else:
            run_open = False
        if not row.startswith("-"):
            head_line += 1
    return "\n".join(out) if out else diff


def callees(
    lines: list[str], first: int, last: int, start: int, end: int, sources: dict[str, str]
) -> list[tuple[str, int, int, str]]:
    """Definitions in changed files of names called inside first..last, nearest the finding first."""
    calls: dict[str, int] = {}
    for number in range(first, last + 1):
        for match in _CALL.finditer(lines[number - 1]):
            name = next(group for group in match.groups() if group)
            if name in _NOT_CALLEES or _defined_name(lines[number - 1]) == name:
                continue
            distance = 0 if start <= number <= end else min(abs(number - start), abs(number - end))
            calls[name] = min(distance, calls.get(name, distance))
    functions: list[tuple[str, int, int, str]] = []
    classes: list[tuple[str, int, int, str]] = []
    for name in sorted(calls, key=lambda item: calls[item]):
        hit = _find_definition(name, sources)
        if hit is None:
            continue
        is_class = re.match(r"\s*(?:export\s+)?(?:default\s+)?class\b", hit[3].split(":", 1)[1])
        bucket = classes if is_class else functions
        if len(bucket) < (MAX_CLASSES if is_class else MAX_CALLEES):
            bucket.append(hit)
    return functions + classes


def _find_definition(name: str, sources: dict[str, str]) -> tuple[str, int, int, str] | None:
    for path in sorted(sources):
        rows = sources[path].splitlines()
        for number, text in enumerate(rows, start=1):
            if _defined_name(text) == name:
                last = min(block_end(rows, number), number + CALLEE_LINES - 1)
                return path, number, last, numbered(rows, number, last)
    return None


def callers(repo: Path, head_sha: str, symbol: str, cache: dict[str, list[str]]) -> tuple[list[str], bool]:
    """Call sites of symbol in the head tree with a few lines of context, and whether all are listed."""
    if not symbol or symbol == "unknown" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        return [], False
    found = run_git(repo, "grep", "-n", "-I", "-F", "-w", "-e", symbol, head_sha, "--")
    if found.returncode == 1:
        return [], True
    if found.returncode != 0:
        return [], False
    sites: list[str] = []
    total = 0
    for row in found.stdout.splitlines():
        parts = row.split(":", 3)
        if len(parts) < 4:
            continue
        _, path, line_no, text = parts
        if _defined_name(text) == symbol or not re.search(rf"\b{re.escape(symbol)}\s*\(", text):
            continue
        total += 1
        if len(sites) >= MAX_CALLERS:
            continue
        rows = _head_lines(repo, head_sha, path, cache)
        number = int(line_no)
        first = max(1, number - CALLER_CONTEXT)
        last = min(len(rows), number + CALLER_CONTEXT)
        sites.append(f"FILE {path}:{first}-{last}\n" + numbered(rows, first, last))
    return sites, total <= MAX_CALLERS


def _head_lines(repo: Path, head_sha: str, path: str, cache: dict[str, list[str]]) -> list[str]:
    if path not in cache:
        shown = run_git(repo, "show", f"{head_sha}:{path}")
        cache[path] = shown.stdout.splitlines() if shown.returncode == 0 else []
    return cache[path]
