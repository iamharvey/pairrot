"""Read-only lookups the reviewer can ask for. They never execute repository scripts or touch the network."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pairrot.models import norm_path
from pairrot.redact import strip_secrets
from pairrot.snapshot import run_git

_MAX_READ_LINES = 400
_MAX_SEARCH_HITS = 50


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


class RepoTools:
    def __init__(
        self,
        repo: Path | str,
        base_sha: str,
        head_sha: str,
        *,
        max_calls: int,
        cache: dict[str, str] | None = None,
    ) -> None:
        self.repo = Path(repo)
        self.base_sha = base_sha
        self.head_sha = head_sha
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
        """None once max_calls git lookups have run. A cached answer costs no call."""
        args = arguments or {}
        key = name + "\n" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        with self._lock:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
            if self.calls >= self.max_calls:
                return None
            self.calls += 1
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
        if name == "list_tests":
            return self._list_tests(str(args.get("path") or ""))
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
