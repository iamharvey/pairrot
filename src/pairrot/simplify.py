"""Deterministic gates for simplification findings. They run before the verifier and never execute repo code."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

from pairrot.complexity_budget import references
from pairrot.context import block_end, defined_name, enclosing
from pairrot.models import Category, DroppedCandidate, Finding, Verdict, Verification, norm_path
from pairrot.policy import Policy, matches
from pairrot.snapshot import run_git
from pairrot.tools import looks_like_test

_IMPORT = re.compile(r"^\s*import\s+(.+)$", re.M)
_FROM = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b", re.M)
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
# Distributions whose import name differs from the project name.
_IMPORT_NAMES = {
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "pillow": "PIL",
    "scikit_learn": "sklearn",
    "python_dateutil": "dateutil",
    "opencv_python": "cv2",
    "protobuf": "google",
    "attrs": "attr",
}
MAX_LOCK_TESTS = 5


def imported_modules(code: str) -> set[str]:
    """Top-level module names a Python snippet imports. Relative imports are local and skipped."""
    found: set[str] = set()
    for match in _IMPORT.finditer(code):
        for part in match.group(1).split(","):
            name = part.strip().split(" as ")[0].strip()
            if re.fullmatch(r"[A-Za-z_][\w.]*", name):
                found.add(name.split(".")[0])
    for match in _FROM.finditer(code):
        found.add(match.group(1).split(".")[0])
    return found


def _import_name(distribution: str) -> str:
    key = re.sub(r"[-.]+", "_", distribution.strip().lower())
    return _IMPORT_NAMES.get(key, key)


def _show(repo: Path, head_sha: str, path: str) -> str:
    shown = run_git(repo, "show", f"{head_sha}:{path}")
    return shown.stdout if shown.returncode == 0 else ""


def _pyproject_names(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    specs: list[str] = []
    project = data.get("project") or {}
    specs.extend(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs.extend(group or [])
    for group in (data.get("dependency-groups") or {}).values():
        specs.extend(item for item in group or [] if isinstance(item, str))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    specs.extend((poetry.get("dependencies") or {}).keys())
    names: set[str] = set()
    for spec in specs:
        match = _REQUIREMENT.match(str(spec))
        if match:
            names.add(match.group(1))
    return names


def declared_modules(repo: Path, head_sha: str) -> set[str]:
    """Import names the head tree already depends on or defines itself."""
    listed = run_git(repo, "ls-tree", "-r", "--name-only", head_sha)
    paths = listed.stdout.splitlines() if listed.returncode == 0 else []
    distributions: set[str] = set()
    modules: set[str] = set()
    for path in paths:
        name = Path(path).name
        if name == "pyproject.toml":
            distributions |= _pyproject_names(_show(repo, head_sha, path))
        elif name.startswith("requirements") and name.endswith(".txt"):
            for row in _show(repo, head_sha, path).splitlines():
                match = _REQUIREMENT.match(row)
                if match and not row.lstrip().startswith(("#", "-")):
                    distributions.add(match.group(1))
        if path.endswith(".py"):
            parts = Path(path).parts
            top = parts[1] if parts[0] in {"src", "lib"} and len(parts) > 1 else parts[0]
            modules.add(Path(top).stem)
    return modules | {_import_name(item) for item in distributions}


def new_dependencies(finding: Finding, declared: set[str], file_source: str) -> list[str]:
    if not finding.replacement or not finding.location.file.endswith(".py"):
        return []
    known = set(sys.stdlib_module_names) | declared | imported_modules(file_source)
    return sorted(name for name in imported_modules(finding.replacement) if name not in known)


def locking_tests(repo: Path, head_sha: str, symbol: str) -> list[str]:
    """Test files in the head tree that mention the symbol. This is a static proxy for coverage."""
    if not symbol or symbol == "unknown" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        return []
    found = run_git(repo, "grep", "-l", "-I", "-F", "-w", "-e", symbol, head_sha, "--")
    if found.returncode != 0:
        return []
    tests: list[str] = []
    for row in found.stdout.splitlines():
        path = row.split(":", 1)[1] if ":" in row else row
        if looks_like_test(path):
            tests.append(norm_path(path))
    return sorted(tests)[:MAX_LOCK_TESTS]


def fit_range(finding: Finding, rows: list[str]) -> Finding:
    """Widen the range to the whole definition when the replacement rewrites that definition.

    Reviewers often send a full new function but mark only part of the old one.
    """
    code = [row for row in (finding.replacement or "").splitlines() if row.strip()]
    at = next((index for index, row in enumerate(code) if not row.lstrip().startswith("@")), None)
    name = defined_name(code[at]) if at is not None else None
    if name and block_end(code, at + 1) == at + 1:
        return finding
    start, end = finding.location.start_line, finding.location.end_line
    span = enclosing(rows, start, end) if name and rows else None
    if span is None or defined_name(rows[span[0] - 1]) != name:
        return finding
    first_line = span[0]
    if code and code[0].lstrip().startswith("@"):
        while first_line > 1 and rows[first_line - 2].lstrip().startswith("@"):
            first_line -= 1
    if (first_line, span[1]) == (start, end):
        return finding
    location = finding.location.model_copy(update={"start_line": first_line, "end_line": span[1]})
    return finding.model_copy(update={"location": location})


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def splice_problem(finding: Finding, rows: list[str], repo: Path, head_sha: str) -> str | None:
    """Why the replacement, spliced into the head file, cannot work. None when it parses and leaves no dangling names."""
    start, end = finding.location.start_line, finding.location.end_line
    after = "\n".join([*rows[: start - 1], *(finding.replacement or "").splitlines(), *rows[end:]]) + "\n"
    try:
        before_tree = ast.parse("\n".join(rows) + "\n")
    except SyntaxError:
        return None
    try:
        after_tree = ast.parse(after)
    except SyntaxError as exc:
        return f"替换后 {finding.location.file} 无法解析（第 {exc.lineno} 行：{exc.msg}），行号范围和替换代码对不上"
    loaded = {node.id for node in ast.walk(after_tree) if isinstance(node, ast.Name)}
    path = norm_path(finding.location.file)
    for name in sorted(_top_level_names(before_tree) - _top_level_names(after_tree)):
        sites = [f"{site[0]}:{site[1]}" for site in references(repo, head_sha, name, include_tests=True) if norm_path(site[0]) != path]
        if name in loaded:
            sites.insert(0, f"{path} 剩下的代码")
        if sites:
            return f"替换删掉了 {name}，但 {'、'.join(sites[:3])} 仍在使用"
    return None


def _reject(finding: Finding, check: str, reason: str) -> Finding:
    return finding.model_copy(update={"verification": Verification(verdict=Verdict.rejected, checked=[check], reason=reason)})


def gate_simplifications(
    findings: list[Finding],
    *,
    policy: Policy,
    repo: Path,
    head_sha: str,
    sources: dict[str, str],
) -> tuple[list[Finding], list[DroppedCandidate]]:
    """Drop extension points, reject new dependencies, and record which tests lock the behavior."""
    if not any(finding.category == Category.complexity for finding in findings):
        return findings, []
    declared = declared_modules(repo, head_sha)
    kept: list[Finding] = []
    dropped: list[DroppedCandidate] = []
    for finding in findings:
        if finding.category != Category.complexity:
            kept.append(finding)
            continue
        if matches(finding.location.file, policy.extension_points):
            dropped.append(
                DroppedCandidate(
                    reason="extension point",
                    category=finding.category.value,
                    file=finding.location.file,
                    trigger=finding.trigger,
                )
            )
            continue
        source = sources.get(norm_path(finding.location.file), "")
        rows = source.splitlines()
        python = finding.location.file.endswith(".py") and finding.replacement is not None and bool(rows)
        if python:
            finding = fit_range(finding, rows)
        tests = locking_tests(repo, head_sha, finding.symbol)
        finding = finding.model_copy(update={"locked_by": tests})
        added = new_dependencies(finding, declared, source)
        if added:
            finding = _reject(finding, "new_dependency", "替换引入了项目没有声明的依赖：" + "、".join(added))
        elif python:
            problem = splice_problem(finding, rows, repo, head_sha)
            if problem:
                finding = _reject(finding, "splice", problem)
        kept.append(finding)
    return kept, dropped
