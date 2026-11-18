"""Render a report for places other than GitHub: one Markdown comment, or SonarQube external issues."""

from __future__ import annotations

from pairrot.models import Category, Finding, RunReport, Severity, norm_path
from pairrot.publish_github import _LABELS, _comment_body, plan_publication

_ATTRIBUTE = {
    Category.logic_error: "LOGICAL",
    Category.error_handling: "COMPLETE",
    Category.security: "TRUSTWORTHY",
    Category.performance: "EFFICIENT",
}
_TYPE = {Category.security: "VULNERABILITY", Category.performance: "CODE_SMELL"}
_QUALITY = {Category.security: "SECURITY", Category.performance: "MAINTAINABILITY"}
_RULE_SEVERITY = {Severity.S1: "CRITICAL", Severity.S2: "MAJOR", Severity.S3: "MINOR"}
_IMPACT_SEVERITY = {Severity.S1: "HIGH", Severity.S2: "MEDIUM", Severity.S3: "LOW"}


def to_markdown(report: RunReport) -> str:
    """The GitHub summary followed by every confirmed comment, since there are no inline threads."""
    every_line: dict[str, set[int]] = {}
    for item in report.confirmed:
        lines = every_line.setdefault(norm_path(item.location.file), set())
        lines.update(range(item.location.start_line, item.location.end_line + 1))
    summary = plan_publication(report, every_line, set()).summary
    bodies = [
        f"#### `{item.location.file}:{item.location.start_line}`\n\n{_comment_body(item)}"
        for item in report.confirmed
    ]
    if not bodies:
        return summary
    return summary + "\n\n### 意见详情\n\n" + "\n\n".join(bodies)


def _rule_id(finding: Finding) -> str:
    return f"pairrot.{finding.category.value}.{finding.severity.value}"


def to_sonar(report: RunReport) -> dict:
    """SonarQube generic issue format (10.3+). Only confirmed findings become issues."""
    rules: dict[str, dict] = {}
    issues: list[dict] = []
    for finding in report.confirmed:
        rule = _rule_id(finding)
        label = _LABELS.get(finding.category.value, finding.category.value)
        rules.setdefault(
            rule,
            {
                "id": rule,
                "name": f"pairrot {label} {finding.severity.value}",
                "description": f"pairrot 复核确认的{label}问题，严重度 {finding.severity.value}。",
                "engineId": "pairrot",
                "cleanCodeAttribute": _ATTRIBUTE.get(finding.category, "LOGICAL"),
                "type": _TYPE.get(finding.category, "BUG"),
                "severity": _RULE_SEVERITY[finding.severity],
                "impacts": [
                    {
                        "softwareQuality": _QUALITY.get(finding.category, "RELIABILITY"),
                        "severity": _IMPACT_SEVERITY[finding.severity],
                    }
                ],
            },
        )
        parts = [finding.title or finding.trigger, f"条件：{finding.trigger}"]
        if finding.consequence:
            parts.append(f"结果：{finding.consequence}")
        if finding.suggestion:
            parts.append(f"建议：{finding.suggestion}")
        issues.append(
            {
                "ruleId": rule,
                "primaryLocation": {
                    "message": " ".join(parts),
                    "filePath": norm_path(finding.location.file),
                    "textRange": {
                        "startLine": finding.location.start_line,
                        "endLine": max(finding.location.start_line, finding.location.end_line),
                    },
                },
            }
        )
    return {"rules": list(rules.values()), "issues": issues}
