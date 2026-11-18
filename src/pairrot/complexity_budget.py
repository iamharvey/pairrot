"""Count what a change adds. No model call. Thresholds come from [complexity_budget] in the base REVIEW.toml."""

from __future__ import annotations

import re
from pathlib import Path

from pairrot.context import defined_name
from pairrot.models import ComplexityStats, norm_path
from pairrot.policy import Policy
from pairrot.snapshot import Snapshot, file_diff, run_git
from pairrot.tools import looks_like_test

_ENV_READ = re.compile(r"os\.environ|\bgetenv\s*\(|process\.env\.")
MAX_SYMBOLS = 100

_LIMIT_TEXT = {
    "max_net_added": "净增行数",
    "max_new_files": "新文件",
    "max_new_symbols": "新增类和函数",
    "max_single_use": "全仓只用一次或没用到的新符号",
    "max_new_env_reads": "新增环境变量读取",
}


def _changed_rows(diff: str) -> tuple[list[str], list[str]]:
    added: list[str] = []
    removed: list[str] = []
    for row in diff.splitlines():
        if row.startswith("+++") or row.startswith("---"):
            continue
        if row.startswith("+"):
            added.append(row[1:])
        elif row.startswith("-"):
            removed.append(row[1:])
    return added, removed


def references(repo: Path, head_sha: str, name: str, *, include_tests: bool = False) -> list[tuple[str, int, str]]:
    """Mentions of name at head as (path, line, text), not counting its own definition. Tests only on request."""
    found = run_git(repo, "grep", "-n", "-I", "-F", "-w", "-e", name, head_sha, "--")
    if found.returncode != 0:
        return []
    used = re.compile(
        rf"\b{re.escape(name)}\s*\(|\.{re.escape(name)}\b|\bimport\b.*\b{re.escape(name)}\b"
        rf"|[=(,\[]\s*{re.escape(name)}\b|\b{re.escape(name)}\s*[,)\]:]|@{re.escape(name)}\b"
        rf"|\b(?:if|elif|while|not|and|or|return|assert)\s+{re.escape(name)}\b"
    )
    sites: list[tuple[str, int, str]] = []
    for row in found.stdout.splitlines():
        parts = row.split(":", 3)
        if len(parts) < 4 or (looks_like_test(parts[1]) and not include_tests):
            continue
        text = parts[3].strip()
        if defined_name(parts[3]) == name or text.startswith("#") or not used.search(text):
            continue
        sites.append((parts[1], int(parts[2]), text))
    return sites


def top_level_name(row: str) -> str | None:
    """Name defined by an unindented def or class. Methods are left out because their names collide."""
    return defined_name(row) if row[:1] not in {" ", "\t"} else None


def changed_rows(repo: Path, snapshot: Snapshot, path: str) -> tuple[list[str], list[str]]:
    return _changed_rows(file_diff(repo, snapshot.base_sha, snapshot.head_sha, path, context=0))


def complexity_stats(repo: Path, snapshot: Snapshot) -> ComplexityStats:
    net = 0
    new_files: list[str] = []
    defined: list[str] = []
    removed_names: set[str] = set()
    env_added = 0
    env_removed = 0
    for span in snapshot.reviewed:
        path = norm_path(span.path)
        if looks_like_test(path):
            continue
        net += span.added - span.deleted
        if span.status.startswith("A"):
            new_files.append(path)
        added, removed = changed_rows(repo, snapshot, span.path)
        for row in added:
            name = top_level_name(row)
            if name and not name.startswith("__") and name not in defined:
                defined.append(name)
        for row in removed:
            name = defined_name(row)
            if name:
                removed_names.add(name)
        env_added += sum(1 for row in added if _ENV_READ.search(row))
        env_removed += sum(1 for row in removed if _ENV_READ.search(row))
    symbols = [name for name in defined if name not in removed_names]
    single = [name for name in symbols[:MAX_SYMBOLS] if len(references(repo, snapshot.head_sha, name)) <= 1]
    return ComplexityStats(
        net_added=net,
        new_files=new_files,
        new_symbols=symbols,
        single_use=single,
        new_env_reads=max(0, env_added - env_removed),
    )


def over_budget(stats: ComplexityStats, policy: Policy) -> list[str]:
    values = {
        "max_net_added": stats.net_added,
        "max_new_files": len(stats.new_files),
        "max_new_symbols": len(stats.new_symbols),
        "max_single_use": len(stats.single_use),
        "max_new_env_reads": stats.new_env_reads,
    }
    return [
        f"{_LIMIT_TEXT[name]} {values[name]}，上限 {limit}"
        for name, limit in policy.budget_limits
        if values[name] > limit
    ]
