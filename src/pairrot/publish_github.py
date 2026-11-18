"""Post a pairrot report to a GitHub pull request.

Inline comments are a pull request review. The summary is one issue comment
that later runs update in place. A failed HTTP call raises PublishError and
does not change the review status into passed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from pairrot.models import CATEGORY_FROM_LABEL, Category, ComplexityStats, Finding, RunReport, Severity, norm_path

SUMMARY_MARKER = "<!-- pairrot-summary -->"
OVERRIDE_LABEL = "pairrot-override"
_FINGERPRINT = re.compile(r"<!-- pairrot:([0-9a-f]+) -->")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_INLINE_S2_LIMIT = 5
_SEVERITY_ORDER = {Severity.S1: 0, Severity.S2: 1, Severity.S3: 2}
_LABELS = {category.value: label for label, category in CATEGORY_FROM_LABEL.items()}


class PublishError(RuntimeError):
    pass


@dataclass
class InlineComment:
    path: str
    line: int
    body: str
    fingerprint: str
    start_line: int | None = None


@dataclass
class Override:
    """Who applied the override label to this head. reset means new commits removed it."""

    actor: str | None = None
    reset: bool = False


@dataclass
class Publication:
    summary: str
    inline: list[InlineComment]
    check: str = "ok"


def load_report(path: Path | str) -> RunReport:
    return RunReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def right_side_lines(patch: str | None) -> set[int]:
    """Line numbers on the new side of a GitHub pull-request patch."""
    lines: set[int] = set()
    current: int | None = None
    for raw in (patch or "").splitlines():
        hunk = _HUNK.match(raw)
        if hunk:
            current = int(hunk.group(1))
            continue
        if current is None or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        lines.add(current)
        current += 1
    return lines


def diff_lines_from_files(files: list[dict]) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    for item in files:
        path = norm_path(str(item.get("filename") or ""))
        if not path:
            continue
        found[path] = right_side_lines(item.get("patch"))
    return found


def _anchor(finding: Finding, lines: set[int]) -> int | None:
    start = finding.location.start_line
    end = finding.location.end_line
    if end < start:
        start, end = end, start
    for line in range(end, start - 1, -1):
        if line in lines:
            return line
    return None


def _public_title(finding: Finding) -> str:
    text = (finding.title or finding.trigger).strip().splitlines()[0]
    return text[:40] or finding.category.value


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _lock_note(finding: Finding) -> str:
    if finding.locked_by:
        return "锁定测试：" + "、".join(f"`{path}`" for path in finding.locked_by)
    return "锁定测试：无，采纳前先补测试锁定现有行为"


def _simplification(finding: Finding, suggestable: bool) -> str:
    replacement = finding.replacement or ""
    fence = _fence(replacement)
    if suggestable and not replacement:
        block = f"{fence}suggestion\n{fence}"
    elif suggestable:
        block = f"{fence}suggestion\n{replacement}\n{fence}"
    elif finding.replacement is None and finding.edits:
        block = "需要一起改：\n" + "\n".join(f"- {edit}" for edit in finding.edits)
    elif replacement:
        block = f"替换为：\n\n{fence}\n{replacement}\n{fence}"
    else:
        block = "替换为：删除这段代码"
    return (
        f"\n\n{block}\n\n"
        f"等价：{finding.equivalence.strip() or '未说明'}\n\n"
        f"性能：{finding.performance_note.strip() or '未说明'}\n\n"
        f"{_lock_note(finding)}"
    )


def _comment_body(finding: Finding, *, suggestable: bool = False) -> str:
    label = _LABELS.get(finding.category.value, finding.category.value)
    title = _public_title(finding)
    consequence = finding.consequence.strip() or "未说明结果"
    suggestion = finding.suggestion.strip() or "无"
    level = "必须处理\n\n" if finding.level == "must_fix" else ""
    body = (
        f"<!-- pairrot:{finding.fingerprint} -->\n"
        f"{level}"
        f"**[{finding.severity.value} {label}]** {title}\n\n"
        f"条件：{finding.trigger.strip()}\n\n"
        f"结果：{consequence}\n\n"
        f"建议：{suggestion}"
    )
    if finding.category == Category.complexity:
        body += _simplification(finding, suggestable)
    return body


def _suggestable(finding: Finding, lines: set[int]) -> bool:
    """A suggestion block replaces the commented range, so every line of it must be on the diff."""
    if finding.category != Category.complexity or finding.replacement is None:
        return False
    start, end = finding.location.start_line, finding.location.end_line
    return all(line in lines for line in range(start, end + 1))


def _inline(finding: Finding, path: str, line: int, lines: set[int]) -> InlineComment:
    suggestable = _suggestable(finding, lines)
    start = finding.location.start_line if suggestable else None
    return InlineComment(
        path=path,
        line=finding.location.end_line if suggestable else line,
        body=_comment_body(finding, suggestable=suggestable),
        fingerprint=finding.fingerprint,
        start_line=start if start is not None and start < finding.location.end_line else None,
    )


def _one_line(finding: Finding, note: str) -> str:
    label = _LABELS.get(finding.category.value, finding.category.value)
    location = f"{finding.location.file}:{finding.location.start_line}"
    notes = [note] if note else []
    if finding.category == Category.complexity and finding.edits:
        notes.append("需要一起改：" + "；".join(finding.edits))
    if finding.category == Category.complexity and not finding.locked_by:
        notes.append("无测试锁定，采纳前先补测试")
    suffix = f"（{'；'.join(notes)}）" if notes else ""
    return f"- [{finding.severity.value} {label}] `{location}` {_public_title(finding)}{suffix}"


def _check_state(report: RunReport, override: Override) -> str:
    if not report.fail_check:
        return "ok"
    return "overridden" if override.actor else "failed"


def _check_line(report: RunReport, override: Override) -> str:
    reasons = "、".join(report.fail_reasons) or "存在必须处理的意见"
    if report.fail_check and override.actor:
        return f"已人工放行：{reasons}。@{override.actor} 加了 `{OVERRIDE_LABEL}` 标签"
    if report.fail_check:
        text = f"失败：{reasons}"
        if override.reset:
            text += f"。有新提交，之前的 `{OVERRIDE_LABEL}` 标签已撤销"
        return text
    if report.fail_on_must_fix or report.fail_reasons:
        return "通过：没有必须处理的意见"
    return "不因意见失败"


def _plan_leveled(
    report: RunReport,
    diff_lines: dict[str, set[int]],
    posted: set[str],
    override: Override,
) -> Publication:
    inline: list[InlineComment] = []
    must_lines: list[str] = []
    recommend_lines: list[str] = []
    pending_human: list[str] = []
    confirmed = sorted(report.confirmed, key=lambda item: _SEVERITY_ORDER[item.severity])
    for finding in confirmed:
        path = norm_path(finding.location.file)
        line = _anchor(finding, diff_lines.get(path, set()))
        if finding.level == "must_fix":
            if finding.fingerprint in posted:
                must_lines.append(_one_line(finding, "已在行内评论"))
            elif line is None:
                must_lines.append(_one_line(finding, "不在本次 diff 的可评论行上"))
            else:
                inline.append(_inline(finding, path, line, diff_lines.get(path, set())))
                must_lines.append(_one_line(finding, ""))
            continue
        recommend_lines.append(_one_line(finding, ""))
    for finding in report.summary.s1_undecided_pending_human:
        pending_human.append(_one_line(finding, "待人工确认"))
    seen_pending = {item.fingerprint for item in report.summary.s1_undecided_pending_human}
    for finding in report.undecided:
        if finding.fingerprint in seen_pending:
            continue
        pending_human.append(_one_line(finding, "未判定"))
    return _publication(
        report,
        inline,
        must="\n".join(must_lines) or "- 无",
        recommend="\n".join(recommend_lines) or "- 无",
        pending="\n".join(pending_human) or "- 无",
        leveled=True,
        override=override,
    )


def plan_publication(
    report: RunReport,
    diff_lines: dict[str, set[int]],
    posted: set[str],
    override: Override | None = None,
) -> Publication:
    if report.policy_source == "base":
        return _plan_leveled(report, diff_lines, posted, override or Override())
    inline: list[InlineComment] = []
    summary_only: list[str] = []
    pending_human: list[str] = []
    s2_slots = _INLINE_S2_LIMIT
    confirmed = sorted(report.confirmed, key=lambda item: _SEVERITY_ORDER[item.severity])
    for finding in confirmed:
        path = norm_path(finding.location.file)
        line = _anchor(finding, diff_lines.get(path, set()))
        if finding.fingerprint in posted:
            continue
        if finding.severity == Severity.S3:
            summary_only.append(_one_line(finding, ""))
            continue
        if line is None:
            summary_only.append(_one_line(finding, "不在本次 diff 的可评论行上"))
            continue
        if finding.severity == Severity.S2 and s2_slots <= 0:
            summary_only.append(_one_line(finding, "超出行内预算"))
            continue
        if finding.severity == Severity.S2:
            s2_slots -= 1
        inline.append(
            InlineComment(
                path=path,
                line=line,
                body=_comment_body(finding),
                fingerprint=finding.fingerprint,
            )
        )
    for finding in report.summary.s1_undecided_pending_human:
        pending_human.append(_one_line(finding, "待人工确认"))
    seen_pending = {item.fingerprint for item in report.summary.s1_undecided_pending_human}
    for finding in report.undecided:
        if finding.fingerprint in seen_pending:
            continue
        pending_human.append(_one_line(finding, "未判定"))

    extra = "\n".join(summary_only) or "- 无"
    pending = "\n".join(pending_human) or "- 无"
    return _publication(report, inline, extra=extra, pending=pending, leveled=False)


def _complexity_block(stats: ComplexityStats) -> str:
    single = "、".join(f"`{name}`" for name in stats.single_use[:20]) or "无"
    exceeded = "；".join(stats.exceeded) or "无"
    return (
        "### 复杂度统计\n"
        f"不含测试文件。净增 {stats.net_added} 行，新文件 {len(stats.new_files)} 个，"
        f"新增类和函数 {len(stats.new_symbols)} 个，新增环境变量读取 {stats.new_env_reads} 处。\n\n"
        f"全仓只用一次或没用到的新符号：{single}\n\n"
        f"超出预算：{exceeded}\n\n"
        + ("" if stats.complete else "统计没有做完（时间用尽），上面的数字是下限。\n\n")
    )


def _publication(
    report: RunReport,
    inline: list[InlineComment],
    *,
    extra: str = "- 无",
    must: str = "- 无",
    recommend: str = "- 无",
    pending: str,
    leveled: bool,
    override: Override | None = None,
) -> Publication:
    override = override or Override()
    reviewed = "\n".join(f"- `{item.path}`" for item in report.reviewed) or "- 无"
    unreviewed = "\n".join(
        f"- `{item.path}`：{item.reason}" for item in report.unreviewed
    ) or "- 无"
    reasons = "、".join(report.partial_reasons) if report.partial_reasons else "无"
    error = report.error or "无"
    check = f"检查：{_check_line(report, override)}\n\n" if leveled else ""
    if leveled:
        findings_block = (
            f"规则：{report.policy_note or '无'}\n\n"
            f"### 必须处理\n{must}\n\n"
            f"### 建议\n{recommend}\n\n"
        )
    else:
        findings_block = f"### 未挂到行内的意见\n{extra}\n\n"
    if report.complexity is not None:
        findings_block += _complexity_block(report.complexity)
    summary = (
        f"{SUMMARY_MARKER}\n"
        f"## pairrot 评审\n\n"
        f"状态：**{report.status.value}**\n\n"
        f"{check}"
        f"base `{report.base_sha}`\n\n"
        f"head `{report.head_sha}`\n\n"
        f"已审查：\n{reviewed}\n\n"
        f"未审查：\n{unreviewed}\n\n"
        f"需求：{report.coverage.ticket}\n\n"
        f"CI：{report.coverage.ci}\n\n"
        f"驳回 {len(report.rejected)} 条，不展示在意见里。\n\n"
        f"{findings_block}"
        f"### 未判定\n{pending}\n\n"
        f"partial 原因：{reasons}\n\n"
        f"错误：{error}\n\n"
        f"耗时 {report.elapsed_seconds}s，token {report.tokens.total}。"
    )
    if len(summary) > 60000:
        summary = summary[:60000] + "\n\n…摘要已截断"
    return Publication(summary=summary, inline=inline, check=_check_state(report, override))


class GitHubClient:
    def __init__(
        self,
        token: str,
        repository: str,
        *,
        api_url: str = "https://api.github.com",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if "/" not in repository:
            raise PublishError("GITHUB_REPOSITORY 需要是 owner/name")
        self.owner, self.repo = repository.split("/", 1)
        self.api = api_url.rstrip("/")
        self.token = token
        self.transport = transport

    def request(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        try:
            with httpx.Client(timeout=30, transport=self.transport) as client:
                response = client.request(
                    method,
                    self.api + path,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                        "User-Agent": "pairrot",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise PublishError(f"GitHub 请求失败: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            detail = response.text[:300].replace(self.token, "[REDACTED]")
            raise PublishError(f"GitHub 返回 {response.status_code}: {detail}")
        return response

    def paginate(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while page <= 20:
            separator = "&" if "?" in path else "?"
            response = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                break
            page += 1
        return items


def posted_fingerprints(comments: list[dict]) -> set[str]:
    found: set[str] = set()
    for comment in comments:
        found.update(_FINGERPRINT.findall(str(comment.get("body") or "")))
    return found


def _summary_comment_id(comments: list[dict]) -> int | None:
    for comment in comments:
        if SUMMARY_MARKER in str(comment.get("body") or ""):
            raw = comment.get("id")
            if isinstance(raw, int):
                return raw
    return None


def _review_comment(item: InlineComment) -> dict:
    comment = {"path": item.path, "line": item.line, "side": "RIGHT", "body": item.body}
    if item.start_line is not None:
        comment["start_line"] = item.start_line
        comment["start_side"] = "RIGHT"
    return comment


def _label_actor(events: list[dict]) -> str | None:
    actor = None
    for event in events:
        label = event.get("label") or {}
        if event.get("event") == "labeled" and label.get("name") == OVERRIDE_LABEL:
            actor = (event.get("actor") or {}).get("login")
    return actor


def read_override(client: GitHubClient, pr: int, *, new_commits: bool) -> Override:
    """An override covers one head. New commits remove the label so it has to be applied again."""
    issue = f"/repos/{client.owner}/{client.repo}/issues/{pr}"
    labels = client.paginate(f"{issue}/labels")
    if not any(item.get("name") == OVERRIDE_LABEL for item in labels):
        return Override()
    if new_commits:
        client.request("DELETE", f"{issue}/labels/{OVERRIDE_LABEL}")
        return Override(reset=True)
    return Override(actor=_label_actor(client.paginate(f"{issue}/events")) or "unknown")


def publish_github(
    report: RunReport,
    pr: int,
    *,
    client: GitHubClient,
    new_commits: bool = False,
) -> Publication:
    files = client.paginate(f"/repos/{client.owner}/{client.repo}/pulls/{pr}/files")
    review_comments = client.paginate(f"/repos/{client.owner}/{client.repo}/pulls/{pr}/comments")
    issue_comments = client.paginate(f"/repos/{client.owner}/{client.repo}/issues/{pr}/comments")
    override = read_override(client, pr, new_commits=new_commits) if report.policy_source == "base" else Override()
    planned = plan_publication(
        report,
        diff_lines_from_files(files),
        posted_fingerprints(review_comments),
        override,
    )
    if planned.inline:
        client.request(
            "POST",
            f"/repos/{client.owner}/{client.repo}/pulls/{pr}/reviews",
            {
                "commit_id": report.head_sha,
                "event": "COMMENT",
                "body": "pairrot 把确认过的意见挂在变更行上。总览在 pairrot 摘要评论里。",
                "comments": [_review_comment(item) for item in planned.inline],
            },
        )
    existing = _summary_comment_id(issue_comments)
    if existing is None:
        client.request(
            "POST",
            f"/repos/{client.owner}/{client.repo}/issues/{pr}/comments",
            {"body": planned.summary},
        )
    else:
        client.request(
            "PATCH",
            f"/repos/{client.owner}/{client.repo}/issues/comments/{existing}",
            {"body": planned.summary},
        )
    return planned


def publish_from_env(
    report_path: Path | str,
    pr: int,
    repository: str | None = None,
    token: str | None = None,
    *,
    new_commits: bool = False,
) -> Publication:
    repo = repository or os.environ.get("GITHUB_REPOSITORY") or ""
    secret = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not repo or not secret:
        raise PublishError("缺少 GITHUB_REPOSITORY 或 GITHUB_TOKEN")
    api = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    client = GitHubClient(secret, repo, api_url=api)
    return publish_github(load_report(report_path), pr, client=client, new_commits=new_commits)

