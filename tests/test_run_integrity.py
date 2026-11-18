"""Truncated facts, the wall clock in git-only stages, duplicate findings, posted must_fix, and engine-scoped reuse."""

from __future__ import annotations

import json
from pathlib import Path

from pairrot import pipeline
from pairrot.budget import Clock
from pairrot.facts import HEADER, TRUNCATED, design_facts, facts_for, is_complete
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
    Verdict,
    Verification,
)
from pairrot.pipeline import review_change
from pairrot.policy import policy_from_toml
from pairrot.publish_github import plan_publication
from pairrot.review import load_sources
from pairrot.simplify import gate_simplifications
from pairrot.snapshot import take_snapshot
from pairrot.verify import _context
from tests.conftest import commit, init_repo, write

_DESIGN_TOML = "[simplicity]\nenabled = true\n\n[complexity_budget]\nmax_single_use = 100\n"


def _completion(payload: dict) -> Completion:
    return Completion(
        message=ChatMessage(role="assistant", content=json.dumps(payload, ensure_ascii=False)),
        prompt_tokens=1,
        completion_tokens=1,
    )


def _many_helpers(tmp_path: Path, toml: str = "") -> tuple[Path, str, str]:
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "def keep():\n    return 1\n")
    if toml:
        write(repo, "REVIEW.toml", toml)
    base = commit(repo, "base")
    helpers = "".join(f"\n\ndef helper_{index}(value):\n    return value + {index}\n" for index in range(12))
    write(repo, "app.py", "def keep():\n    return 1\n" + helpers)
    write(repo, "other.py", "def other_helper(value):\n    return value\n")
    tip = commit(repo, "head")
    return repo, base, tip


def test_truncated_facts_keep_whole_rows_and_reach_the_verifier_as_incomplete(tmp_path: Path):
    repo, base, tip = _many_helpers(tmp_path)
    snapshot = take_snapshot(repo, base, tip)
    sources = load_sources(repo, snapshot)
    full = design_facts(repo, snapshot, sources)
    assert is_complete(full)
    short = design_facts(repo, snapshot, sources, limit=len(HEADER) + len(TRUNCATED) + 120)

    assert not is_complete(short)
    assert short.endswith(TRUNCATED)
    assert set(short.splitlines()) - {TRUNCATED} <= set(full.splitlines())
    assert "other.py" not in short
    assert facts_for(short, "other.py") == f"{HEADER}\n{TRUNCATED}"

    finding = Finding(
        fingerprint="f",
        category=Category.complexity,
        severity=Severity.S3,
        location=Location(file="other.py", start_line=1, end_line=2),
        trigger="other_helper 没有调用方",
    )
    context = _context(finding, sources, {}, [], base_sha=base, head_sha=tip, facts=short)
    pack = json.loads(context.split("<repo_data>\n")[2].split("\n</repo_data>")[0])
    [facts] = [item for item in pack if item["source"] == "facts"]
    assert facts["complete"] is False


def test_out_of_time_stops_git_only_stages_and_marks_the_run_partial(tmp_path: Path):
    repo, base, tip = _many_helpers(tmp_path, _DESIGN_TOML)
    model = FakeModel(lambda *args: _completion({"findings": []}))
    report = review_change(repo, base, tip, tmp_path / "out", llm=model, clock=Clock(lambda: 10_000))

    assert model.calls == []
    assert report.status == RunStatus.partial
    assert "墙钟达到上限" in report.partial_reasons
    assert report.complexity is not None and not report.complexity.complete
    assert report.complexity.single_use == []

    snapshot = take_snapshot(repo, base, tip)
    sources = load_sources(repo, snapshot)
    assert design_facts(repo, snapshot, sources, stop=lambda: True) == f"{HEADER}\n{TRUNCATED}"
    finding = Finding(
        fingerprint="g",
        category=Category.complexity,
        severity=Severity.S3,
        location=Location(file="app.py", start_line=4, end_line=5),
        trigger="t",
        replacement="",
    )
    kept, dropped = gate_simplifications(
        [finding], policy=policy_from_toml(_DESIGN_TOML), repo=repo, head_sha=tip, sources=sources, stop=lambda: True
    )
    assert kept == [finding] and dropped == []


def test_a_finding_reported_twice_is_verified_once(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "def add(a, b):\n    return a + b\n")
    base = commit(repo, "base")
    write(repo, "app.py", "def add(a, b):\n    return a - b\n")
    tip = commit(repo, "head")
    finding = {
        "category": "logic_error",
        "severity": "S2",
        "file": "app.py",
        "start_line": 2,
        "end_line": 2,
        "trigger": "subtracts when it should add",
        "consequence": "callers get the wrong total",
        "suggestion": "use addition",
        "snippet": "return a - b",
        "introduced_in": "this_change",
    }
    richer = {**finding, "evidence": [{"type": "diff", "ref": "app.py:2", "note": "operator flipped"}]}
    verified: list[str] = []

    def handler(messages, model):
        if "独立验证" in (messages[0].content or ""):
            verified.append(messages[1].content or "")
            return _completion({"verdict": "confirmed", "reason": "成立", "checked": ["path_reachable"]})
        return _completion({"findings": [finding, richer]})

    report = review_change(repo, base, tip, tmp_path / "out", llm=FakeModel(handler))

    assert len(verified) == 1
    [confirmed] = report.confirmed
    assert len(confirmed.evidence) == 1
    assert [item.reason for item in report.dropped] == ["duplicate fingerprint"]


def test_a_posted_must_fix_stays_in_the_summary_while_the_check_fails():
    finding = Finding(
        fingerprint="posted",
        category=Category.logic_error,
        severity=Severity.S1,
        location=Location(file="app.py", start_line=2, end_line=2),
        title="总额算反了",
        trigger="subtracts when it should add",
        verification=Verification(verdict=Verdict.confirmed, checked=[], reason="成立"),
        introduced_in=IntroducedIn.this_change,
        level="must_fix",
    )
    report = RunReport(
        status=RunStatus.passed,
        base_sha="a" * 40,
        head_sha="b" * 40,
        prompt_version="v",
        budget=Budget(),
        coverage=Coverage(ci="无", ticket="无"),
        confirmed=[finding],
        policy_source="base",
        fail_on_must_fix=True,
        fail_check=True,
        fail_reasons=["存在必须处理的意见"],
    )
    planned = plan_publication(report, {"app.py": {2}}, {"posted"})

    assert planned.inline == []
    assert planned.check == "failed"
    must = planned.summary.split("### 必须处理\n")[1].split("\n\n")[0]
    assert "`app.py:2` 总额算反了（已在行内评论）" in must


def test_a_change_to_pairrot_itself_does_not_replay_old_reports(tmp_path: Path, monkeypatch):
    repo, base, tip = _many_helpers(tmp_path)
    model = FakeModel(lambda *args: _completion({"findings": []}))
    out = tmp_path / "out"
    review_change(repo, base, tip, out, llm=model)
    assert review_change(repo, base, tip, out, llm=model).reused

    monkeypatch.setattr(pipeline, "engine_digest", lambda: "another-build")
    assert not review_change(repo, base, tip, out, llm=model).reused
