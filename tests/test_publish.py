"""GitHub review comments stay on confirmed diff lines and can be updated."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

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
    TokenUsage,
    Verification,
    Verdict,
)
from pairrot.publish_github import (
    GitHubClient,
    diff_lines_from_files,
    plan_publication,
    publish_github,
    right_side_lines,
)


def _finding(fingerprint: str, severity: Severity, line: int, category: Category = Category.logic_error) -> Finding:
    return Finding(
        fingerprint=fingerprint,
        category=category,
        severity=severity,
        location=Location(file="src/app.py", start_line=line, end_line=line),
        trigger=f"trigger {fingerprint}",
        consequence="callers see the wrong result",
        suggestion="compare the quantity",
        verification=Verification(verdict=Verdict.confirmed, checked=["可达路径"], reason="成立"),
        introduced_in=IntroducedIn.this_change,
    )


def _report(confirmed: list[Finding], undecided: list[Finding] | None = None) -> RunReport:
    return RunReport(
        status=RunStatus.partial,
        base_sha="a" * 40,
        head_sha="b" * 40,
        prompt_version="phase1-correctness-v2",
        budget=Budget(),
        coverage=Coverage(ci="无 CI 结果", ticket="未关联需求，未做业务意图核对"),
        confirmed=confirmed,
        undecided=undecided or [],
        partial_reasons=["工具调用达到上限"],
        tokens=TokenUsage(total=10),
        elapsed_seconds=1.5,
    )


def test_right_side_lines_skip_deletions():
    patch = "\n".join(
        [
            "@@ -1,3 +10,4 @@",
            " context",
            "-removed",
            "+added",
            " trailing",
        ]
    )
    assert right_side_lines(patch) == {10, 11, 12}


def test_inline_budget_skips_posted_s3_and_lines_outside_the_diff():
    findings = [
        _finding("s1", Severity.S1, 10),
        *[_finding(f"s2-{index}", Severity.S2, 11) for index in range(6)],
        _finding("s3", Severity.S3, 12, Category.error_handling),
        _finding("off", Severity.S1, 99),
        _finding("s2-0", Severity.S2, 11),
    ]
    # The duplicate fingerprint object is a second instance with the same id as s2-0.
    findings[-1] = _finding("already", Severity.S2, 12)
    planned = plan_publication(
        _report(findings),
        {"src/app.py": {10, 11, 12}},
        {"already"},
    )
    posted_inline = [item.fingerprint for item in planned.inline]
    assert posted_inline[0] == "s1"
    assert posted_inline[1:] == [f"s2-{index}" for index in range(5)]
    assert "s3" not in posted_inline
    assert "off" not in posted_inline
    assert "already" not in posted_inline
    assert "S3 只进摘要" in planned.summary
    assert "不在本次 diff 的可评论行上" in planned.summary
    assert "超出行内预算" in planned.summary
    assert "<!-- pairrot-summary -->" in planned.summary


def test_publish_creates_review_and_updates_existing_summary(tmp_path: Path):
    report = _report([_finding("new", Severity.S2, 4)])
    path = tmp_path / "report.json"
    path.write_text(report.model_dump_json(), encoding="utf-8")
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/pulls/7/files"):
            return httpx.Response(200, json=[{"filename": "src/app.py", "patch": "@@ -1 +4,1 @@\n+added"}])
        if request.url.path.endswith("/pulls/7/comments"):
            return httpx.Response(200, json=[{"body": "<!-- pairrot:old --> already"}])
        if request.url.path.endswith("/issues/7/comments") and request.method == "GET":
            return httpx.Response(200, json=[{"id": 42, "body": "<!-- pairrot-summary -->\nold"}])
        return httpx.Response(200, json={"id": 1})

    client = GitHubClient("secret-token", "acme/pairrot", transport=httpx.MockTransport(handler))
    planned = publish_github(report, 7, client=client)
    assert [item.fingerprint for item in planned.inline] == ["new"]
    review = next(body for method, path, body in calls if method == "POST" and path.endswith("/reviews"))
    assert review is not None
    assert review["event"] == "COMMENT"
    assert review["commit_id"] == "b" * 40
    assert review["comments"][0]["path"] == "src/app.py"
    assert review["comments"][0]["line"] == 4
    assert review["comments"][0]["side"] == "RIGHT"
    assert "<!-- pairrot:new -->" in review["comments"][0]["body"]
    assert "secret-token" not in review["comments"][0]["body"]
    update = next(body for method, path, body in calls if method == "PATCH")
    assert update is not None
    assert "<!-- pairrot-summary -->" in update["body"]
    assert diff_lines_from_files([{"filename": "src/app.py", "patch": "@@ -1 +4,1 @@\n+added"}])["src/app.py"] == {4}
