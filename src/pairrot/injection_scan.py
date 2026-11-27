"""Deterministic prompt-injection findings. No model call, and exclude does not apply."""

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
from pairrot.policy import Policy, injection_weakened, matches
from pairrot.secrets_scan import added_lines
from pairrot.snapshot import Snapshot, file_diff, run_git

_OPEN = "<" + "repo_data>"
_CLOSE = "</" + "repo_data>"
_SNIPPET = 160
_OVERRIDE = re.compile(
    r"(?i)("
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|above|prior|earlier)\s+(?:instructions|prompts|rules|directives)"
    r"|disregard\s+(?:the\s+)?(?:previous|above|system|prior)"
    r"|forget\s+(?:all\s+|your\s+)?(?:previous|prior)\s+(?:instructions|rules|prompts)"
    r"|you\s+are\s+now\s+"
    r"|忽略(?:以上|之前|先前|上面)的?(?:指令|提示|规则|要求)"
    r"|不要(?:遵守|执行)(?:以上|之前|上面)"
    r"|从现在起你是"
    r")"
)
_CRED = re.compile(
    r"(?i)(\.env\b|~/\.ssh|\.aws/credentials|OPENAI_API_KEY|GITHUB_TOKEN|AWS_SECRET_ACCESS_KEY|ANTHROPIC_API_KEY)"
)
_SEND = re.compile(r"(?i)(\bcurl\b|\bwebhook\b|\bupload\b|\bPOST\b|requests\.post|httpx\.(?:post|request))")
_REVIEWER = re.compile(r"(?i)(ai\s+reviewer|pairrot|代码评审|code\s+review\s+bot)")
_BYPASS = re.compile(
    r"(?i)(不要报告|don't\s+report|do\s+not\s+report|标记为安全|mark\s+as\s+safe|ignore\s+this\s+finding)"
)
_HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_MD_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_S1 = {"override", "exfiltration", "hidden_text"}
_TITLES = {
    "override": "疑似提示词注入",
    "exfiltration": "疑似凭据外传",
    "reviewer_bypass": "疑似绕过评审",
    "hidden_text": "疑似隐藏指令",
    "tag_escape": "疑似打断数据标签",
}


def visible(text: str) -> str:
    """Escape invisible and bidi characters so a comment cannot carry the instruction into another agent."""
    shown: list[str] = []
    for char in text:
        if _HIDDEN.match(char):
            shown.append(f"\\u{ord(char):04x}")
        else:
            shown.append(char)
    snippet = "".join(shown)[:_SNIPPET]
    return snippet.replace("```", "'''")


def _finding(path: str, line_no: int, rule: str, text: str, introduced: IntroducedIn) -> Finding:
    snippet = visible(text)
    return Finding(
        fingerprint=fingerprint(Category.security.value, path, f"{rule}:{line_no}", snippet),
        category=Category.security,
        severity=Severity.S1 if rule in _S1 else Severity.S2,
        location=Location(file=path, start_line=line_no, end_line=line_no),
        title=_TITLES[rule],
        trigger=f"命中 {rule}。\n\n```\n{snippet}\n```",
        consequence="这段文字会被交给 agent，可能改掉它的规则或把凭据送出去。",
        suggestion="删掉这段指令。指令文件不要写覆盖规则、外传凭据，或藏起来的文字。",
        evidence=[Evidence(type="diff", ref=f"{path}:{line_no}", note="确定性规则 prompt_injection")],
        verification=Verification(
            verdict=Verdict.confirmed,
            checked=["injection_pattern"],
            reason=f"匹配 {rule}。评论里的片段已转义。",
        ),
        introduced_in=introduced,
        snippet=snippet,
        symbol=f"{rule}:{line_no}",
        rule=rule,
    )


def _hidden_comment(text: str) -> bool:
    for comment in _MD_COMMENT.findall(text):
        if _OVERRIDE.search(comment) or re.search(r"(?i)system prompt|忽略", comment):
            return True
    return False


def _collect(path: str, rows: list[tuple[int, str]], policy: Policy, introduced: IntroducedIn) -> list[Finding]:
    findings: list[Finding] = []
    instruction = matches(path, policy.instruction_paths())
    by_no = {line_no: text for line_no, text in rows}
    emitted: set[int] = set()
    for line_no, text in rows:
        if instruction and _OVERRIDE.search(text):
            findings.append(_finding(path, line_no, "override", text, introduced))
        if instruction and line_no not in emitted:
            window_no = [number for number in (line_no - 1, line_no, line_no + 1) if number in by_no]
            window = "\n".join(by_no[number] for number in window_no)
            if _CRED.search(window) and _SEND.search(window):
                emitted.update(window_no)
                findings.append(_finding(path, line_no, "exfiltration", text, introduced))
        if _REVIEWER.search(text) and _BYPASS.search(text):
            findings.append(_finding(path, line_no, "reviewer_bypass", text, introduced))
        if _HIDDEN.search(text) or _hidden_comment(text):
            findings.append(_finding(path, line_no, "hidden_text", text, introduced))
        if _OPEN in text or _CLOSE in text:
            findings.append(_finding(path, line_no, "tag_escape", text, introduced))
    live = set(policy.live_rules("prompt_injection")) if policy.active else set(_TITLES)
    if policy.active:
        return [item for item in findings if item.rule in live]
    return findings


def scan_text(path: str, text: str, policy: Policy, introduced: IntroducedIn) -> list[Finding]:
    rows = list(enumerate(text.splitlines(), 1))
    return _collect(norm_path(path), rows, policy, introduced)


def scan_added_injections(repo: Path, snapshot: Snapshot, policy: Policy) -> list[Finding]:
    """Added lines of every text file in the change, including excluded and budget-cut files."""
    if not policy.scans_prompt_injection():
        return []
    findings: list[Finding] = []
    seen: set[str] = set()
    for span in list(snapshot.reviewed) + list(snapshot.unreviewed):
        if span.reason == "二进制文件":
            continue
        path = norm_path(span.path)
        if path in seen:
            continue
        seen.add(path)
        diff = file_diff(repo, snapshot.base_sha, snapshot.head_sha, span.path, context=3)
        findings.extend(
            _collect(path, added_lines(diff), policy, IntroducedIn.this_change)
        )
    return findings


def _line_of(text: str, needle: str) -> tuple[int, str]:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number, line
    return 1, needle


def guard_findings(repo: Path, rev: str, reasons: list[str]) -> list[Finding]:
    """One must-fix finding per way the head REVIEW.toml weakens the injection check."""
    if not reasons:
        return []
    shown = run_git(repo, "show", f"{rev}:REVIEW.toml")
    text = shown.stdout if shown.returncode == 0 else ""
    findings: list[Finding] = []
    for reason in reasons:
        needle = reason.split()[-1] if reason.startswith("删掉了") else reason
        if reason.startswith("子规则"):
            needle = reason.split()[1]
        line_no, line = _line_of(text, needle)
        snippet = visible(line)
        findings.append(
            Finding(
                fingerprint=fingerprint(Category.security.value, "REVIEW.toml", f"policy_guard:{reason}", snippet),
                category=Category.security,
                severity=Severity.S1,
                location=Location(file="REVIEW.toml", start_line=line_no, end_line=line_no),
                title="【危险】关闭了提示词注入检查",
                trigger=reason,
                consequence="关掉之后，指令文件里的注入不会再挡合并。",
                suggestion="恢复提示词注入检查，或由维护者加 pairrot-override 确认这次关闭。",
                evidence=[Evidence(type="diff", ref=f"REVIEW.toml:{line_no}", note="确定性规则 policy_guard")],
                verification=Verification(
                    verdict=Verdict.confirmed,
                    checked=["policy_guard"],
                    reason=reason,
                ),
                introduced_in=IntroducedIn.this_change,
                snippet=snippet,
                symbol=f"policy_guard:{reason}",
                rule="override",
                level="must_fix",
            )
        )
    return findings


def weakened_against(base: Policy, head: Policy) -> list[str]:
    return injection_weakened(base, head)
