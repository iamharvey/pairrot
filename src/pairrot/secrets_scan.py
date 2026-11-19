"""Deterministic secret findings on added lines. The secret value is never stored."""

from __future__ import annotations

import re
from pathlib import Path

from pairrot.models import (
    Category,
    Evidence,
    Finding,
    IntroducedIn,
    Location,
    Severity,
    Verification,
    Verdict,
    fingerprint,
    norm_path,
)
from pairrot.redact import secret_name
from pairrot.rules import secret_rule
from pairrot.snapshot import Snapshot, file_diff

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def added_lines(diff_text: str) -> list[tuple[int, str]]:
    """New-file line number and text for each added diff line."""
    found: list[tuple[int, str]] = []
    current: int | None = None
    for raw in diff_text.splitlines():
        hunk = _HUNK.match(raw)
        if hunk:
            current = int(hunk.group(1))
            continue
        if current is None or raw.startswith("\\"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            found.append((current, raw[1:]))
            current += 1
            continue
        if raw.startswith("-"):
            continue
        current += 1
    return found


def secret_finding(path: str, line_no: int, label: str, symbol: str) -> Finding:
    snippet = "[REDACTED]"
    return Finding(
        fingerprint=fingerprint(Category.security.value, path, symbol, snippet),
        category=Category.security,
        severity=Severity.S2,
        location=Location(file=path, start_line=line_no, end_line=line_no),
        title="疑似密钥" if symbol else "新增了疑似密钥",
        trigger=f"赋值 {label}，值已隐藏。",
        consequence="明文密钥会进入仓库历史，轮换之前一直有效。",
        suggestion="去掉明文，改为运行时注入的环境变量或密钥管理。",
        evidence=[Evidence(type="diff", ref=f"{path}:{line_no}", note="确定性规则 secrets")],
        verification=Verification(
            verdict=Verdict.confirmed,
            checked=["secret_pattern"],
            reason="赋值匹配密钥模式。评论不包含原文。",
        ),
        introduced_in=IntroducedIn.this_change,
        snippet=snippet,
        symbol=symbol,
        rule=secret_rule(label),
    )


def scan_added_secrets(repo: Path, snapshot: Snapshot) -> list[Finding]:
    findings: list[Finding] = []
    for span in snapshot.reviewed:
        diff = file_diff(repo, snapshot.base_sha, snapshot.head_sha, span.path, context=3)
        counts: dict[str, int] = {}
        path = norm_path(span.path)
        for line_no, text in added_lines(diff):
            label = secret_name(text)
            if not label:
                continue
            counts[label] = counts.get(label, 0) + 1
            symbol = label if counts[label] == 1 else f"{label}:{counts[label]}"
            found = secret_finding(path, line_no, label, symbol)
            findings.append(found.model_copy(update={"title": "新增了疑似密钥", "trigger": f"新增赋值 {label}，值已隐藏。"}))
    return findings
