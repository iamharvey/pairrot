"""Deterministic risky-pattern findings. Off unless REVIEW.toml enables the table."""

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
from pairrot.policy import Policy, matches
from pairrot.secrets_scan import added_lines
from pairrot.snapshot import Snapshot, file_diff

_REMOTE = re.compile(r"(?i)\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba)?sh\b|\biex\s*\(\s*iwr\b")
_TLS = re.compile(
    r"(?i)verify\s*=\s*False\b|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|"
    r"NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*0|\bcurl\s+-k\b|\bcurl\s+[^\n]*\s-k\b"
)
_HTTP = re.compile(r"(?i)\bhttp://([^/\s\"'`<>]+)")
_LOCAL = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "example.com", "example.org", "example.net"}
_EVAL = re.compile(r"\beval\s*\(")
_EXEC = re.compile(r"\bexec\s*\(")
_PICKLE = re.compile(r"\bpickle\.loads\s*\(")
_SYSTEM = re.compile(r"\bos\.system\s*\(")
_SHELL = re.compile(r"\bshell\s*=\s*True\b")
_YAML = re.compile(r"\byaml\.load\s*\(")
_CHMOD = re.compile(r"(?i)\bchmod\s+777\b|\b0o777\b")
_WRITE_ALL = re.compile(r"(?i)permissions\s*:\s*write-all")
_SECRET_LOG = re.compile(r"(?i)(?:echo\b|run\s*:)[^\n]*secrets\.")
_PR_HEAD = re.compile(r"github\.event\.pull_request\.head|refs/pull/.*/head")
_WORKFLOW = (".github/workflows/*", ".github/workflows/**")
_LINE_RULES = (
    ("remote_script", _REMOTE),
    ("tls_off", _TLS),
    ("loose_perms", _CHMOD),
)
_TITLES = {
    "remote_script": "下载后直接执行脚本",
    "tls_off": "关掉了证书校验",
    "plaintext_http": "使用明文 http",
    "dangerous_call": "使用了危险调用",
    "loose_perms": "权限对所有用户可写",
    "ci_workflow": "工作流权限过宽",
}


def _host(raw: str) -> str:
    host = raw.lower().rstrip(".")
    if host.startswith("["):
        return host
    return host.split(":", 1)[0]


def _dangerous(text: str) -> bool:
    if any(pattern.search(text) for pattern in (_EVAL, _EXEC, _PICKLE, _SYSTEM, _SHELL)):
        return True
    return _YAML.search(text) is not None and "SafeLoader" not in text and "safe_load" not in text


def _http(text: str) -> bool:
    for match in _HTTP.finditer(text):
        host = _host(match.group(1))
        if host in _LOCAL or host.endswith(".example"):
            continue
        return True
    return False


def _finding(path: str, line_no: int, rule: str, text: str, introduced: IntroducedIn) -> Finding:
    snippet = text.strip()[:160]
    return Finding(
        fingerprint=fingerprint(Category.security.value, path, f"{rule}:{line_no}", snippet),
        category=Category.security,
        severity=Severity.S2 if rule == "ci_workflow" else Severity.S3,
        location=Location(file=path, start_line=line_no, end_line=line_no),
        title=_TITLES[rule],
        trigger=f"命中 {rule}。\n\n```\n{snippet}\n```",
        consequence="这是确定性匹配到的不安全写法，还没有判断它能不能被利用。",
        suggestion="换成校验证书、https、不执行外部脚本，或收窄工作流权限。",
        evidence=[Evidence(type="diff", ref=f"{path}:{line_no}", note="确定性规则 risky_patterns")],
        verification=Verification(
            verdict=Verdict.confirmed,
            checked=["risky_pattern"],
            reason=f"匹配 {rule}。",
        ),
        introduced_in=introduced,
        snippet=snippet,
        symbol=f"{rule}:{line_no}",
        rule=rule,
    )


def _ci_hits(path: str, rows: list[tuple[int, str]]) -> list[tuple[int, str, str]]:
    if not matches(path, _WORKFLOW):
        return []
    text = "\n".join(line for _, line in rows)
    hits: list[tuple[int, str, str]] = []
    first = rows[0][0] if rows else 1

    def line_of(pattern: re.Pattern[str]) -> tuple[int, str]:
        for number, line in rows:
            if pattern.search(line):
                return number, line
        return first, text[:160]

    if "pull_request_target" in text and _PR_HEAD.search(text):
        number, line = line_of(re.compile(r"pull_request_target"))
        hits.append((number, "ci_workflow", line))
    if _WRITE_ALL.search(text):
        number, line = line_of(_WRITE_ALL)
        hits.append((number, "ci_workflow", line))
    if _SECRET_LOG.search(text):
        number, line = line_of(_SECRET_LOG)
        hits.append((number, "ci_workflow", line))
    return hits


def _collect(path: str, rows: list[tuple[int, str]], policy: Policy, introduced: IntroducedIn) -> list[Finding]:
    if not policy.scans_risky_patterns():
        return []
    findings: list[Finding] = []
    for line_no, text in rows:
        for rule, pattern in _LINE_RULES:
            if pattern.search(text):
                findings.append(_finding(path, line_no, rule, text, introduced))
        if _http(text):
            findings.append(_finding(path, line_no, "plaintext_http", text, introduced))
        if _dangerous(text):
            findings.append(_finding(path, line_no, "dangerous_call", text, introduced))
    for line_no, rule, text in _ci_hits(path, rows):
        findings.append(_finding(path, line_no, rule, text, introduced))
    live = set(policy.live_rules("risky_patterns"))
    return [item for item in findings if item.rule in live]


def scan_text(path: str, text: str, policy: Policy, introduced: IntroducedIn) -> list[Finding]:
    return _collect(norm_path(path), list(enumerate(text.splitlines(), 1)), policy, introduced)


def scan_added_risky(repo: Path, snapshot: Snapshot, policy: Policy) -> list[Finding]:
    """Added lines of files that survived exclude and the budget. Overrides apply later."""
    if not policy.scans_risky_patterns():
        return []
    findings: list[Finding] = []
    for span in snapshot.reviewed:
        path = norm_path(span.path)
        diff = file_diff(repo, snapshot.base_sha, snapshot.head_sha, span.path, context=0)
        findings.extend(_collect(path, added_lines(diff), policy, IntroducedIn.this_change))
    return findings
