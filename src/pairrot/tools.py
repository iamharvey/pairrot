"""Read-only tools. They never execute repository scripts or touch the network."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pairrot.models import norm_path
from pairrot.redact import strip_secrets
from pairrot.snapshot import literal_pathspec, run_git

_MAX_READ_LINES = 400
_MAX_SEARCH_HITS = 50
_MAX_BLAME_LINES = 200
_MAX_LOG = 50
_MAX_TEXT = 100_000

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a line range from rev 'base' or 'head' only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "rev": {"type": "string", "enum": ["base", "head"]},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "rev", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search the head tree with git grep. Does not search base and does not use a language server.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "Approximate references by a fixed-string search of the head tree.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Read recent commits. With a path, history of that file at head; otherwise commits from base to head.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_blame",
            "description": "Blame a line range at head.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tests",
            "description": "Find related tests by common filename conventions in the head tree.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ci_results",
            "description": "Read the local CI JSON file passed on the CLI. Never calls a CI service.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticket",
            "description": "Read the local ticket text passed on the CLI. Never calls a ticket system.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def looks_like_test(path: str) -> bool:
    name = Path(path).name.lower()
    parts = path.lower().replace("\\", "/").split("/")
    if any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts):
        return True
    if name.startswith("test_") or name.endswith("_test.py") or name.endswith("_test.go"):
        return True
    if ".test." in name or ".spec." in name or name.endswith("test.java"):
        return True
    return False


def _read_local_text(path: Path | None, *, missing: str) -> str:
    if path is None or not Path(path).is_file():
        return missing
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n[截断]"
    return text


class RepoTools:
    def __init__(
        self,
        repo: Path | str,
        base_sha: str,
        head_sha: str,
        *,
        ticket_path: Path | None,
        ci_path: Path | None,
        max_calls: int,
        cache: dict[str, str] | None = None,
    ) -> None:
        self.repo = Path(repo)
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.ticket_path = ticket_path
        self.ci_path = ci_path
        self.max_calls = max_calls
        self.calls = 0
        self.cache = cache if cache is not None else {}
        self._lock = threading.Lock()

    def execute(self, name: str, arguments: dict[str, Any] | None) -> str:
        result = self.try_execute(name, arguments)
        if result is None:
            return "工具调用达到上限，未执行"
        return result

    def try_execute(self, name: str, arguments: dict[str, Any] | None) -> str | None:
        args = arguments or {}
        key = name + "\n" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock:
            if self.calls >= self.max_calls:
                return None
            self.calls += 1
            cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            result = self._dispatch(name, args)
        except Exception as exc:
            result = f"工具执行失败: {exc}"
        result = strip_secrets(result)
        if not result:
            result = "无输出"
        with self._lock:
            self.cache[key] = result
        return result

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "read_file":
            return self._read_file(args)
        if name == "search_code":
            return self._search(str(args.get("pattern") or ""), fixed=False)
        if name == "find_references":
            return self._search(str(args.get("symbol") or ""), fixed=True)
        if name == "git_log":
            return self._log(args)
        if name == "git_blame":
            return self._blame(args)
        if name == "list_tests":
            return self._list_tests(str(args.get("path") or ""))
        if name == "get_ci_results":
            return _read_local_text(self.ci_path, missing="无 CI 结果")
        if name == "get_ticket":
            return _read_local_text(self.ticket_path, missing="未关联需求，未做业务意图核对")
        return "未知工具"

    def _safe_path(self, path: str) -> str:
        norm = norm_path(path)
        if not norm or ".." in norm.split("/"):
            raise ValueError("路径不合法")
        return norm

    def _read_file(self, args: dict[str, Any]) -> str:
        rev = str(args.get("rev") or "")
        if rev not in {"base", "head"}:
            return "rev 只能是 base 或 head"
        sha = self.base_sha if rev == "base" else self.head_sha
        path = self._safe_path(str(args.get("path") or ""))
        try:
            start = int(args.get("start_line"))
            end = int(args.get("end_line"))
        except (TypeError, ValueError):
            return "行号无效"
        if start < 1:
            start = 1
        if end < start:
            end = start
        truncated = False
        if end - start + 1 > _MAX_READ_LINES:
            end = start + _MAX_READ_LINES - 1
            truncated = True
        completed = run_git(self.repo, "show", f"{sha}:{path}")
        if completed.returncode != 0:
            return "文件在该版本不存在"
        lines = completed.stdout.splitlines()
        chunk = []
        for number in range(start, min(end, len(lines)) + 1):
            chunk.append(f"{number}:{lines[number - 1]}")
        if not chunk:
            return "指定范围没有内容"
        text = "\n".join(chunk)
        if truncated:
            text += "\n[截断]"
        return text

    def _search(self, pattern: str, *, fixed: bool) -> str:
        if not pattern.strip() or len(pattern) > 200:
            return "pattern 无效"
        command = ["grep", "-n", "-I"]
        if fixed:
            command.extend(["-F", "-w"])
        else:
            command.append("-E")
        command.extend(["-e", pattern, self.head_sha, "--"])
        completed = run_git(self.repo, *command)
        if completed.returncode == 1:
            return "无匹配"
        if completed.returncode != 0:
            return "搜索失败: " + completed.stderr.strip()
        lines = completed.stdout.splitlines()[:_MAX_SEARCH_HITS]
        return "\n".join(lines) if lines else "无匹配"

    def _log(self, args: dict[str, Any]) -> str:
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, _MAX_LOG))
        path = args.get("path")
        if path:
            safe = self._safe_path(str(path))
            completed = run_git(
                self.repo,
                "log",
                "--oneline",
                "-n",
                str(limit),
                self.head_sha,
                "--",
                literal_pathspec(safe),
            )
        else:
            completed = run_git(
                self.repo,
                "log",
                "--oneline",
                "-n",
                str(limit),
                f"{self.base_sha}..{self.head_sha}",
            )
        if completed.returncode != 0:
            return "git log 失败: " + completed.stderr.strip()
        return completed.stdout.strip() or "无提交"

    def _blame(self, args: dict[str, Any]) -> str:
        path = self._safe_path(str(args.get("path") or ""))
        try:
            start = int(args.get("start_line"))
            end = int(args.get("end_line"))
        except (TypeError, ValueError):
            return "行号无效"
        if start < 1:
            start = 1
        if end < start:
            end = start
        if end - start + 1 > _MAX_BLAME_LINES:
            end = start + _MAX_BLAME_LINES - 1
        completed = run_git(
            self.repo,
            "blame",
            "-L",
            f"{start},{end}",
            self.head_sha,
            "--",
            literal_pathspec(path),
        )
        if completed.returncode != 0:
            return "git blame 失败: " + completed.stderr.strip()
        return completed.stdout.strip() or "无输出"

    def _list_tests(self, path: str) -> str:
        safe = self._safe_path(path)
        completed = run_git(self.repo, "ls-tree", "-r", "--name-only", self.head_sha)
        if completed.returncode != 0:
            return "无法列出文件"
        stem = Path(safe).stem.lower()
        found = []
        for line in completed.stdout.splitlines():
            if stem and stem in line.lower() and looks_like_test(line):
                found.append(line)
        if not found:
            return "未找到相关测试文件"
        return "\n".join(found[:40])
