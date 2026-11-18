"""Pin two commits and list the change set that will actually be reviewed."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pairrot.models import FileSpan, LineRange

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)

LOCK_BASENAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "npm-shrinkwrap.json",
        "cargo.lock",
        "gemfile.lock",
        "poetry.lock",
        "composer.lock",
        "go.sum",
        "pipfile.lock",
        "packages.lock.json",
        "uv.lock",
        "bun.lock",
        "bun.lockb",
        "podfile.lock",
        "flake.lock",
        "pubspec.lock",
        "shrinkwrap.yaml",
        "pdm.lock",
    }
)

_VENDOR_PARTS = frozenset({"vendor", "node_modules", "third_party", "third-party"})
_GENERATED_SUFFIXES = (
    "_generated.py",
    "_generated.go",
    ".pb.go",
    ".pb.cc",
    ".pb.h",
    "_pb2.py",
    "_pb2.pyi",
    ".g.dart",
    ".min.js",
    ".min.css",
    ".designer.cs",
)


class GitError(RuntimeError):
    pass


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Read-only git invocation. Never uses a shell and never runs repo scripts."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def git_text(repo: Path, *args: str) -> str:
    completed = run_git(repo, *args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise GitError(detail)
    return completed.stdout


def resolve_sha(repo: Path, rev: str) -> str:
    return git_text(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def literal_pathspec(path: str) -> str:
    """Keep brackets and globs as a literal path. Git otherwise treats them as wildcards."""
    return ":(literal)" + path.replace("\\", "/")


def merge_base(repo: Path, base_sha: str, head_sha: str) -> str:
    """Three-dot base. Falls back to base_sha when the histories do not meet."""
    completed = run_git(repo, "merge-base", base_sha, head_sha)
    found = completed.stdout.strip()
    if completed.returncode != 0 or not found:
        return base_sha
    return found


def file_diff(
    repo: Path,
    base_sha: str,
    head_sha: str,
    path: str,
    *,
    context: int = 3,
) -> str:
    completed = run_git(
        repo,
        "diff",
        f"-U{context}",
        base_sha,
        head_sha,
        "--",
        literal_pathspec(path),
    )
    if completed.returncode != 0:
        return completed.stderr.strip()
    return completed.stdout


def skip_reason(path: str) -> str | None:
    name = path.replace("\\", "/").split("/")[-1].lower()
    if name in LOCK_BASENAMES or name.endswith(".lock") or name.endswith(".lockb"):
        return "锁文件"
    parts = [part.lower() for part in path.replace("\\", "/").split("/") if part]
    if any(part in _VENDOR_PARTS for part in parts):
        return "vendor 目录"
    if _is_generated(name, parts):
        return "生成文件"
    if _is_migration_snapshot(path):
        return "迁移快照"
    return None


def _is_generated(name: str, parts: list[str]) -> bool:
    if ".generated." in name or name.endswith(".generated"):
        return True
    if name.endswith(_GENERATED_SUFFIXES):
        return True
    return "generated" in parts or "__generated__" in parts


def _is_migration_snapshot(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    parts = [part for part in low.split("/") if part]
    name = parts[-1] if parts else low
    in_migration = any(part in {"migration", "migrations"} for part in parts)
    if "snapshot" in name and in_migration:
        return True
    if name.endswith(".snap") and in_migration:
        return True
    if name.endswith("modelsnapshot.cs") or name.endswith("model_snapshot.py"):
        return True
    return False


def parse_name_status(blob: str) -> list[tuple[str, str, str | None]]:
    parts = blob.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    records: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        if status[0] in {"R", "C"}:
            if index + 1 >= len(parts):
                break
            old, new = parts[index], parts[index + 1]
            index += 2
            records.append((status, new, old))
            continue
        if index >= len(parts):
            break
        records.append((status, parts[index], None))
        index += 1
    return records


def parse_numstat(blob: str) -> dict[str, tuple[str, str]]:
    parts = blob.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    entries: dict[str, tuple[str, str]] = {}
    index = 0
    while index < len(parts):
        head = parts[index]
        index += 1
        bits = head.split("\t")
        if len(bits) < 3:
            continue
        added, deleted = bits[0], bits[1]
        path = "\t".join(bits[2:])
        if path == "":
            if index + 1 >= len(parts):
                break
            _old, new = parts[index], parts[index + 1]
            index += 2
            entries[new] = (added, deleted)
        else:
            entries[path] = (added, deleted)
    return entries


def parse_ranges(diff_text: str) -> list[LineRange]:
    ranges: list[LineRange] = []
    for match in _HUNK.finditer(diff_text):
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count <= 0 or start <= 0:
            continue
        ranges.append(LineRange(start=start, end=start + count - 1))
    return ranges


def select_for_review(
    files: list[FileSpan],
    *,
    max_files: int,
    max_changed_lines: int,
) -> tuple[list[FileSpan], list[FileSpan], list[str]]:
    """Walk diff order. Once the next file would exceed a cap, the rest stay out."""
    reviewed: list[FileSpan] = []
    unreviewed: list[FileSpan] = []
    used = 0
    cut_reason: str | None = None
    reasons: list[str] = []
    for span in files:
        if cut_reason is None:
            file_lines = span.added + span.deleted
            if len(reviewed) >= max_files:
                cut_reason = f"超过文件数阈值（{max_files}）"
            elif used + file_lines > max_changed_lines:
                cut_reason = f"行数预算已满（{max_changed_lines}），此文件未审"
        if cut_reason is not None:
            if cut_reason not in reasons:
                reasons.append(cut_reason)
            unreviewed.append(span.model_copy(update={"reason": cut_reason}))
            continue
        reviewed.append(span)
        used += span.added + span.deleted
    return reviewed, unreviewed, reasons


@dataclass
class Snapshot:
    base_sha: str
    head_sha: str
    reviewed: list[FileSpan] = field(default_factory=list)
    unreviewed: list[FileSpan] = field(default_factory=list)
    partial: bool = False
    partial_reasons: list[str] = field(default_factory=list)


def take_snapshot(
    repo: Path | str,
    base: str,
    head: str,
    *,
    max_files: int = 40,
    max_changed_lines: int = 2000,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> Snapshot:
    root = Path(repo)
    pinned_base = base_sha or resolve_sha(root, base)
    pinned_head = head_sha or resolve_sha(root, head)
    pinned_base = merge_base(root, pinned_base, pinned_head)
    name_status = parse_name_status(
        git_text(root, "diff", "-z", "--name-status", pinned_base, pinned_head)
    )
    numstat = parse_numstat(
        git_text(root, "diff", "-z", "--numstat", pinned_base, pinned_head)
    )
    skipped: list[FileSpan] = []
    reviewable: list[FileSpan] = []
    for status, path, old in name_status:
        added_s, deleted_s = numstat.get(path, ("0", "0"))
        if added_s == "-" or deleted_s == "-":
            skipped.append(FileSpan(path=path, status=status, reason="二进制文件", old_path=old))
            continue
        try:
            added = int(added_s)
            deleted = int(deleted_s)
        except ValueError:
            added, deleted = 0, 0
        reason = skip_reason(path)
        if reason:
            skipped.append(
                FileSpan(
                    path=path,
                    status=status,
                    added=added,
                    deleted=deleted,
                    reason=reason,
                    old_path=old,
                )
            )
            continue
        ranges = parse_ranges(file_diff(root, pinned_base, pinned_head, path, context=0))
        reviewable.append(
            FileSpan(
                path=path,
                status=status,
                added=added,
                deleted=deleted,
                ranges=ranges,
                old_path=old,
            )
        )
    reviewed, overflow, reasons = select_for_review(
        reviewable, max_files=max_files, max_changed_lines=max_changed_lines
    )
    return Snapshot(
        base_sha=pinned_base,
        head_sha=pinned_head,
        reviewed=reviewed,
        unreviewed=skipped + overflow,
        partial=bool(reasons),
        partial_reasons=reasons,
    )
