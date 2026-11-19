"""Review the working tree, try a local REVIEW.toml, and print findings."""

from __future__ import annotations

import json
from pathlib import Path

from pairrot.cli import _budget_from_env, main
from pairrot.llm import ChatMessage, Completion, FakeModel
from pairrot.models import (
    Budget,
    Category,
    Coverage,
    Finding,
    IntroducedIn,
    Location,
    RunReport,
    RunStatus,
    Severity,
    Verification,
    Verdict,
)
from pairrot.pipeline import review_change
from pairrot.publish_github import plan_publication
from pairrot.snapshot import worktree_commit
from tests.conftest import commit, git, init_repo, write

_SECRET = 'api_key = "supersecretvalue"\n'


def _quiet() -> FakeModel:
    return FakeModel(
        lambda messages, model: Completion(
            message=ChatMessage(role="assistant", content=json.dumps({"findings": []})),
            prompt_tokens=1,
            completion_tokens=1,
        )
    )


def _confirmed() -> Finding:
    return Finding(
        fingerprint="f",
        category=Category.logic_error,
        severity=Severity.S1,
        location=Location(file="app.py", start_line=3, end_line=3),
        title="返回了下界",
        trigger="value 小于 low 时返回 low",
        verification=Verification(verdict=Verdict.confirmed, checked=[], reason="成立"),
        introduced_in=IntroducedIn.this_change,
        level="must_fix",
    )


def test_worktree_commit_includes_uncommitted_files_and_leaves_the_repo(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "one\n")
    base = commit(repo, "base")
    (repo / "app.py").write_text("two\n")
    (repo / "new.py").write_text("three\n")
    snap = worktree_commit(repo)
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == base
    assert git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert (repo / "app.py").read_text(encoding="utf-8") == "two\n"
    names = set(git(repo, "diff", "--name-only", base, snap).stdout.split())
    assert names == {"app.py", "new.py"}


def test_worktree_review_covers_uncommitted_files(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "one\n")
    base = commit(repo, "base")
    (repo / "app.py").write_text("two\n")
    (repo / "new.py").write_text("three\n")
    report = review_change(repo, base, None, tmp_path / "out", llm=_quiet(), worktree=True)
    assert report.head_kind == "worktree"
    assert {span.path for span in report.reviewed} == {"app.py", "new.py"}
    assert "工作区快照" in plan_publication(report, {}, set()).summary


def test_a_local_policy_applies_without_becoming_the_ci_policy(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "x = 1\n")
    base = commit(repo, "base")
    write(repo, "app.py", _SECRET)
    tip = commit(repo, "head")
    policy = tmp_path / "try.toml"
    policy.write_text("[secrets]\nenabled = true\nlevel = \"must_fix\"\n", encoding="utf-8")
    report = review_change(repo, base, tip, tmp_path / "out", llm=_quiet(), policy_file=policy)
    assert report.policy_source == "local"
    assert report.policy_note.startswith("本地规则，CI 不会用这份。")
    assert [item.title for item in report.confirmed] == ["新增了疑似密钥"]
    assert report.confirmed[0].level == "must_fix"


def test_review_prints_findings_and_warns_about_a_local_policy(tmp_path: Path, capsys, monkeypatch):
    report = RunReport(
        status=RunStatus.passed,
        base_sha="a" * 40,
        head_sha="b" * 40,
        head_kind="worktree",
        prompt_version="review-v13",
        budget=Budget(),
        coverage=Coverage(ci="无", ticket="无"),
        confirmed=[_confirmed()],
        policy_source="local",
        policy_note="本地规则，CI 不会用这份。",
    )
    monkeypatch.setattr("pairrot.cli.review_change", lambda *args, **kwargs: report)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = main(
        ["review", "--repo", str(tmp_path), "--base", "HEAD", "--worktree", "--policy", "try.toml", "--out", str(tmp_path / "out")]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "worktree=true" in captured.out
    assert "findings=1" in captured.out
    assert "must_fix app.py:3 返回了下界" in captured.out
    assert "CI 不会用这份" in captured.err


def test_worktree_and_head_together_are_rejected(tmp_path: Path, capsys):
    code = main(["review", "--repo", str(tmp_path), "--base", "HEAD", "--head", "HEAD", "--worktree", "--out", str(tmp_path / "out")])
    assert code == 1
    assert "不要同时传" in capsys.readouterr().err


def test_local_budget_is_larger_unless_ci_or_an_explicit_value(monkeypatch):
    for name in ("GITHUB_ACTIONS", "PAIRROT_MAX_TOOL_CALLS", "PAIRROT_MAX_FILES", "PAIRROT_MAX_CHANGED_LINES", "PAIRROT_WALL_CLOCK_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    local = _budget_from_env(None)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    ci = _budget_from_env(None)
    assert local.max_tool_calls_per_perspective == 8
    assert ci.max_tool_calls_per_perspective == 2
    assert local.max_files == 80 and ci.max_files == 40
    assert local.max_changed_lines == 8000 and ci.max_changed_lines == 2000
    monkeypatch.setenv("PAIRROT_MAX_TOOL_CALLS", "3")
    assert _budget_from_env(None).max_tool_calls_per_perspective == 3
