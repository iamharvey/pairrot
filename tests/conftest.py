"""Shared git fixtures. Commits set identity per invocation and do not touch global config."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed


def init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", branch, "--template=")
    return path


def write(repo: Path, relative: str, text: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", "--", relative)


def commit(repo: Path, message: str) -> str:
    git(
        repo,
        "-c",
        "user.email=pairrot@example.com",
        "-c",
        "user.name=pairrot",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        message,
    )
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def seed(repo: Path, text: str = "seed\n", name: str = "README.md") -> str:
    write(repo, name, text)
    return commit(repo, "init")


def ranges_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """Inclusive ranges overlap when each starts on or before the other ends."""
    return left[0] <= right[1] and right[0] <= left[1]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")
