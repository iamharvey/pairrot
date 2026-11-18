"""Markdown and SonarQube exports of a run report."""

from __future__ import annotations

import json
from pathlib import Path

from pairrot.cli import main
from pairrot.export import to_markdown, to_sonar
from pairrot.pipeline import review_change
from tests.test_engine import _changed_repo, _finding, _scripted

CONFIRM = {"verdict": "confirmed", "reason": "成立", "checked": [], "introduced_in": "this_change"}


def _report(tmp_path: Path):
    body = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a + b\n"
    repo, base, tip = _changed_repo(tmp_path, body)
    findings = [
        _finding(),
        _finding(start_line=6, end_line=6, snippet="return a + b", trigger="adds when it should multiply", severity="S1"),
    ]
    model = _scripted({"findings": findings}, CONFIRM)
    return review_change(repo, base, tip, tmp_path / "out", llm=model), tmp_path / "out" / "report.json"


def test_markdown_lists_every_confirmed_finding_without_a_diff_line_note(tmp_path: Path):
    report, _ = _report(tmp_path)
    text = to_markdown(report)
    assert "## pairrot 评审" in text
    assert "### 意见详情" in text
    assert "#### `app.py:2`" in text and "#### `app.py:6`" in text
    assert "不在本次 diff 的可评论行上" not in text


def test_sonar_export_follows_the_generic_issue_format(tmp_path: Path):
    report, path = _report(tmp_path)
    data = to_sonar(report)
    assert {rule["id"] for rule in data["rules"]} == {"pairrot.logic_error.S1", "pairrot.logic_error.S2"}
    for rule in data["rules"]:
        assert rule["engineId"] == "pairrot"
        assert {"id", "name", "description", "cleanCodeAttribute", "impacts"} <= set(rule)
        assert rule["impacts"][0]["softwareQuality"] == "RELIABILITY"
    issue = next(item for item in data["issues"] if item["ruleId"].endswith("S1"))
    assert issue["primaryLocation"]["filePath"] == "app.py"
    assert issue["primaryLocation"]["textRange"] == {"startLine": 6, "endLine": 6}
    assert "adds when it should multiply" in issue["primaryLocation"]["message"]

    out = tmp_path / "sonar.json"
    assert main(["export", "sonar", "--report", str(path), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == data
