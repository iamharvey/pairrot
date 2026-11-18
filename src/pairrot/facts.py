"""Repository facts for the design pass. Computed with git only, so the model does not have to guess them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from pairrot.complexity_budget import changed_rows, references, top_level_name
from pairrot.context import block_end, defined_name, enclosing
from pairrot.models import norm_path
from pairrot.snapshot import Snapshot, run_git
from pairrot.tools import looks_like_test

MAX_SYMBOLS = 60
MAX_SITES = 6
LIMIT = 6000
_ALIAS = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(?:#.*)?$")
_FLAG = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(True|False)\s*(?:#.*)?$")
HEADER = "下面是 pairrot 用 git 统计的事实，不是推断。"
TRUNCATED = "（事实没有列全：超出了长度或时间上限。没有列出的符号不代表没有调用方或引用。）"


def _assignments(repo: Path, head_sha: str, name: str) -> list[str]:
    """Every place in head, tests included, that sets name. One site means the value never changes."""
    found = run_git(repo, "grep", "-n", "-I", "-w", "-e", name, head_sha, "--")
    if found.returncode != 0:
        return []
    sets = re.compile(rf"\b{re.escape(name)}\s*=(?!=)|setattr\(.*\b{re.escape(name)}\b|setenv\(.*\b{re.escape(name)}\b")
    return [f"{parts[1]}:{parts[2]}" for parts in (row.split(":", 3) for row in found.stdout.splitlines()) if len(parts) == 4 and sets.search(parts[3])]


def _call_sites(repo: Path, head_sha: str, name: str) -> list[str]:
    found = run_git(repo, "grep", "-n", "-I", "-F", "-w", "-e", name, head_sha, "--")
    if found.returncode != 0:
        return []
    sites = []
    for row in found.stdout.splitlines():
        parts = row.split(":", 3)
        if len(parts) < 4 or defined_name(parts[3]) == name:
            continue
        if re.search(rf"\b{re.escape(name)}\s*\(", parts[3]):
            sites.append(f"{parts[1]}:{parts[2]}: {parts[3].strip()}")
    return sites


def _body(rows: list[str], start: int) -> list[str]:
    """Stripped statements of the definition at 1-based start, without its docstring."""
    end = block_end(rows, start)
    body = [row.strip() for row in rows[start:end] if row.strip()]
    if body and body[0][:3] in {'"""', "'''"}:
        quote = body[0][:3]
        closing = 0 if body[0].count(quote) >= 2 and len(body[0]) > 3 else next(
            (index for index, row in enumerate(body[1:], start=1) if quote in row), len(body) - 1
        )
        body = body[closing + 1 :]
    return body


def _head_rows(repo: Path, head_sha: str, path: str, cache: dict[str, list[str]]) -> list[str]:
    if path not in cache:
        shown = run_git(repo, "show", f"{head_sha}:{path}")
        cache[path] = shown.stdout.splitlines() if shown.returncode == 0 else []
    return cache[path]


def _same_body(repo: Path, head_sha: str, path: str, name: str, rows: list[str], cache: dict[str, list[str]]) -> str | None:
    start = next((number for number, row in enumerate(rows, start=1) if defined_name(row) == name), None)
    if start is None:
        return None
    body = _body(rows, start)
    if not body or len(body) > 30:
        return None
    found = run_git(repo, "grep", "-n", "-I", "-F", "-e", body[0], head_sha, "--", "*.py")
    if found.returncode != 0:
        return None
    for row in found.stdout.splitlines():
        parts = row.split(":", 3)
        if len(parts) < 4:
            continue
        other_path, line = parts[1], int(parts[2])
        if norm_path(other_path) == path or looks_like_test(other_path):
            continue
        other_rows = _head_rows(repo, head_sha, other_path, cache)
        span = enclosing(other_rows, line, line)
        if span is None:
            continue
        other = defined_name(other_rows[span[0] - 1])
        if other and _body(other_rows, span[0]) == body:
            return f"{other_path}:{span[0]} {other}"
    return None


def _fit(parts: list[str], limit: int, truncated: bool) -> str:
    """Whole rows only, so a cut never leaves half a row that reads as a complete fact."""
    room = limit - len(TRUNCATED) - 1
    kept: list[str] = []
    size = 0
    for row in parts:
        if size + len(row) + 1 > room:
            truncated = True
            break
        kept.append(row)
        size += len(row) + 1
    if truncated:
        kept.append(TRUNCATED)
    return "\n".join(kept)


def design_facts(
    repo: Path,
    snapshot: Snapshot,
    sources: dict[str, str],
    *,
    limit: int = LIMIT,
    stop: Callable[[], bool] | None = None,
) -> str:
    """Facts for the design pass. stop is polled between git calls; once it returns True the rest is left out and marked."""
    signatures: list[str] = []
    symbols: list[str] = []
    duplicates: list[str] = []
    flags: list[str] = []
    cache: dict[str, list[str]] = {}
    truncated = False
    for span in snapshot.reviewed:
        if stop is not None and stop():
            truncated = True
            break
        path = norm_path(span.path)
        if looks_like_test(path):
            continue
        added, removed = changed_rows(repo, snapshot, span.path)
        old = {top_level_name(row): row.strip() for row in removed if top_level_name(row)}
        new = {top_level_name(row): row.strip() for row in added if top_level_name(row)}
        for row in added:
            alias = _ALIAS.match(row)
            if alias and alias.group(2) not in {"True", "False", "None"} and alias.group(1) not in new:
                new[alias.group(1)] = row.strip()
        for row in added:
            flag = _FLAG.match(row)
            if flag and (len(flags) >= MAX_SYMBOLS or (stop is not None and stop())):
                truncated = True
                break
            if flag:
                sets = _assignments(repo, snapshot.head_sha, flag.group(1))
                reads = references(repo, snapshot.head_sha, flag.group(1))
                where = "、".join(f"{site[0]}:{site[1]}" for site in reads[:MAX_SITES]) or "无"
                flags.append(
                    f"- {path} {flag.group(1)} = {flag.group(2)}：head 全仓赋值 {len(sets)} 处（{'、'.join(sets[:MAX_SITES])}），读取处：{where}"
                )
        rows = sources.get(path, "").splitlines()
        for name, line in new.items():
            if stop is not None and stop():
                truncated = True
                break
            if name in old and old[name] != line:
                if len(signatures) >= MAX_SYMBOLS:
                    truncated = True
                    continue
                sites = _call_sites(repo, snapshot.head_sha, name)
                listed = "；".join(sites[:MAX_SITES]) or "无"
                signatures.append(f"- {path} {name}：旧 `{old[name]}`，新 `{line}`。head 全仓调用点：{listed}")
            elif name not in old and not name.startswith("__"):
                if len(symbols) >= MAX_SYMBOLS:
                    truncated = True
                    continue
                sites = references(repo, snapshot.head_sha, name)
                where = "、".join(f"{site[0]}:{site[1]}" for site in sites[:MAX_SITES])
                symbols.append(f"- {path} {name}：{len(sites)} 处" + (f"（{where}）" if where else ""))
                twin = _same_body(repo, snapshot.head_sha, path, name, rows, cache) if path.endswith(".py") else None
                if twin:
                    duplicates.append(f"- {path} {name} 的实现与 {twin} 完全相同")
    if not (signatures or symbols or duplicates or flags or truncated):
        return ""
    parts = [HEADER]
    if signatures:
        parts += ["签名变化的函数：", *signatures]
    if symbols:
        parts += ["本次新增的函数和类在 head 非测试代码里的引用：", *symbols]
    if duplicates:
        parts += ["与仓库已有函数实现相同的新函数：", *duplicates]
    if flags:
        parts += ["新增的模块级布尔常量（赋值只有 1 处说明它不会变）：", *flags]
    return _fit(parts, limit, truncated)


def is_complete(facts: str) -> bool:
    return TRUNCATED not in facts


def facts_for(facts: str, path: str) -> str:
    """The fact rows that concern path, under the same header.

    Truncated facts always come back with the marker, because a missing row then means "not counted", not "no references".
    """
    mentioned = re.compile(rf"(?<![\w./-]){re.escape(norm_path(path))}(?=[ :])")
    rows = [row for row in facts.splitlines() if row.startswith("- ") and mentioned.search(row)]
    if is_complete(facts):
        return "\n".join([HEADER, *rows]) if rows else ""
    return "\n".join([HEADER, *rows, TRUNCATED])
