"""Wire snapshot, the correctness reviewer, and the independent verifier."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pairrot.budget import BudgetTracker, Clock
from pairrot.complexity_budget import complexity_stats, over_budget
from pairrot.facts import design_facts
from pairrot.llm import LLM, ModelError, client_from_env
from pairrot.models import (
    Budget,
    Coverage,
    Finding,
    ReportSummary,
    RunReport,
    RunStatus,
    Verdict,
    dump_model,
)
from pairrot.policy import Policy, load_policy
from pairrot.review import PROMPT_VERSION, load_sources, prompt_for, review_diff
from pairrot.secrets_scan import scan_added_secrets
from pairrot.simplify import gate_simplifications
from pairrot.snapshot import GitError, merge_base, resolve_sha, take_snapshot
from pairrot.tools import RepoTools
from pairrot.verify import mark_undecided, verify_findings


FACTS_LIMIT = 6000


def reuse_key(
    *,
    base_sha: str,
    head_sha: str,
    prompt_version: str,
    budget: Budget,
    reviewer_model: str | None,
    verifier_model: str | None,
    policy_key: str = "default",
) -> str:
    payload = {
        "base": base_sha,
        "head": head_sha,
        "prompt_version": prompt_version,
        "budget": dump_model(budget),
        "reviewer_model": reviewer_model,
        "verifier_model": verifier_model,
        "policy": policy_key,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def coverage_for(ticket: Path | None, ci: Path | None) -> Coverage:
    if ci is not None and Path(ci).is_file():
        ci_note = "检测到本地 CI 结果，但未参与审查"
    else:
        ci_note = "无 CI 结果"
    if ticket is not None and Path(ticket).is_file():
        ticket_note = "检测到本地需求文件，但未参与审查"
    else:
        ticket_note = "未关联需求，未做业务意图核对"
    return Coverage(ci=ci_note, ticket=ticket_note)


def _summary(undecided: list[Finding]) -> ReportSummary:
    pending = [
        finding
        for finding in undecided
        if finding.severity.value == "S1"
    ]
    if pending:
        note = "S1 未判定留在摘要，待人工确认。S2/S3 未判定不进入确认列表，但仍落盘。"
    else:
        note = "没有待人工确认的 S1 未判定项。S2/S3 未判定不进入确认列表，但仍落盘。"
    return ReportSummary(s1_undecided_pending_human=pending, note=note)


def _decide_status(
    *,
    tracker: BudgetTracker,
    snapshot_partial: bool,
    finding_count: int,
) -> RunStatus:
    if tracker.error and not tracker.produced and finding_count == 0:
        return RunStatus.failed
    if tracker.exhausted or snapshot_partial:
        return RunStatus.partial
    if tracker.error:
        if finding_count:
            return RunStatus.partial
        return RunStatus.failed
    return RunStatus.passed


def _reusable(status: str | None, error: str | None) -> bool:
    """A run cut short by a model or transport error must be retried, not replayed."""
    return status != RunStatus.failed.value and not error


def _write_report(report: RunReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dump_model(report), ensure_ascii=False, indent=2) + "\n"
    latest = out_dir / "report.json"
    latest.write_text(payload, encoding="utf-8")
    if _reusable(report.status.value, report.error) and report.reuse_key and not report.reused:
        runs = out_dir / "runs"
        runs.mkdir(exist_ok=True)
        (runs / f"{report.reuse_key}.json").write_text(payload, encoding="utf-8")
    return latest


def _load_reusable(out_dir: Path, key: str) -> RunReport | None:
    runs = out_dir / "runs"
    if not runs.is_dir():
        return None
    for path in sorted(runs.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("reuse_key") != key:
            continue
        if not _reusable(data.get("status"), data.get("error")):
            continue
        try:
            return RunReport.model_validate(data)
        except ValueError:
            continue
    return None


def _empty_report(
    *,
    status: RunStatus,
    base_sha: str,
    head_sha: str,
    budget: Budget,
    key: str,
    reviewer_model: str | None,
    verifier_model: str | None,
    coverage: Coverage,
    error: str | None,
    elapsed: float,
) -> RunReport:
    return RunReport(
        status=status,
        base_sha=base_sha,
        head_sha=head_sha,
        prompt_version=PROMPT_VERSION,
        budget=budget,
        reuse_key=key,
        reviewer_model=reviewer_model,
        verifier_model=verifier_model,
        coverage=coverage,
        elapsed_seconds=elapsed,
        error=error,
        summary=_summary([]),
    )


def _apply_levels(findings: list[Finding], policy: Policy) -> list[Finding]:
    if not policy.active:
        return findings
    return [
        finding.model_copy(
            update={
                "level": policy.level_for(
                    finding.category,
                    secret=_is_secret(finding),
                    locked=bool(finding.locked_by) and finding.replacement is not None,
                )
            }
        )
        for finding in findings
    ]


def _fail_reasons(policy: Policy, confirmed: list[Finding], exceeded: list[str]) -> list[str]:
    if not policy.active:
        return []
    reasons: list[str] = []
    if policy.fail_on_must_fix and any(item.level == "must_fix" for item in confirmed):
        reasons.append("存在必须处理的意见")
    if exceeded:
        reasons.append("复杂度预算超限：" + "；".join(exceeded))
    return reasons


def _is_secret(finding: Finding) -> bool:
    return finding.verification is not None and "secret_pattern" in finding.verification.checked


def review_change(
    repo: Path | str,
    base: str,
    head: str,
    out_dir: Path | str,
    *,
    ticket: Path | str | None = None,
    ci: Path | str | None = None,
    llm: LLM | None = None,
    budget: Budget | None = None,
    reviewer_model: str | None = None,
    verifier_model: str | None = None,
    reuse: bool = True,
    clock: Clock | None = None,
) -> RunReport:
    root = Path(repo)
    destination = Path(out_dir)
    budget = budget or Budget()
    ticket_path = Path(ticket) if ticket else None
    ci_path = Path(ci) if ci else None
    coverage = coverage_for(ticket_path, ci_path)
    active_clock = clock or Clock()
    try:
        base_sha = resolve_sha(root, base)
        head_sha = resolve_sha(root, head)
        base_sha = merge_base(root, base_sha, head_sha)
    except GitError as exc:
        report = _empty_report(
            status=RunStatus.failed,
            base_sha=base,
            head_sha=head,
            budget=budget,
            key="",
            reviewer_model=reviewer_model,
            verifier_model=verifier_model,
            coverage=coverage,
            error=str(exc),
            elapsed=round(active_clock.elapsed(), 3),
        )
        _write_report(report, destination)
        return report

    verifier_name = verifier_model or reviewer_model
    policy = load_policy(root, base_sha)
    key = reuse_key(
        base_sha=base_sha,
        head_sha=head_sha,
        prompt_version=PROMPT_VERSION,
        budget=budget,
        reviewer_model=reviewer_model,
        verifier_model=verifier_name,
        policy_key=policy.digest(),
    )
    if policy.error:
        report = _empty_report(
            status=RunStatus.failed,
            base_sha=base_sha,
            head_sha=head_sha,
            budget=budget,
            key=key,
            reviewer_model=reviewer_model,
            verifier_model=verifier_name,
            coverage=coverage,
            error=policy.error,
            elapsed=round(active_clock.elapsed(), 3),
        )
        _write_report(report, destination)
        return report
    if reuse:
        existing = _load_reusable(destination, key)
        if existing is not None:
            reused = existing.model_copy(update={"reused": True})
            _write_report(reused, destination)
            return reused

    try:
        snapshot = take_snapshot(
            root,
            base_sha,
            head_sha,
            max_files=budget.max_files,
            max_changed_lines=budget.max_changed_lines,
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except GitError as exc:
        report = _empty_report(
            status=RunStatus.failed,
            base_sha=base_sha,
            head_sha=head_sha,
            budget=budget,
            key=key,
            reviewer_model=reviewer_model,
            verifier_model=verifier_name,
            coverage=coverage,
            error=str(exc),
            elapsed=round(active_clock.elapsed(), 3),
        )
        _write_report(report, destination)
        return report

    tracker = BudgetTracker(budget, active_clock)
    findings: list[Finding] = []
    dropped = []
    if snapshot.reviewed and policy.needs_model():
        if llm is None:
            try:
                llm = client_from_env()
            except ModelError as exc:
                tracker.error = str(exc)
        if llm is not None and tracker.error is None:
            sources = load_sources(root, snapshot)
            evidence_cache: dict[str, str] = {}
            review_tools = RepoTools(
                root,
                snapshot.base_sha,
                snapshot.head_sha,
                ticket_path=ticket_path,
                ci_path=ci_path,
                max_calls=budget.max_tool_calls_per_perspective,
                cache=evidence_cache,
            )
            passes = [(prompt_for(policy), policy.main_categories() if policy.active else None, "")]
            facts = ""
            if policy.active:
                if not policy.main_categories():
                    passes = []
                if policy.design_categories():
                    facts = design_facts(root, snapshot, sources)[:FACTS_LIMIT]
                    passes.append((prompt_for(policy, design=True), policy.design_categories(), facts))

            def run_pass(index: int):
                prompt, allowed, facts = passes[index]
                tools = review_tools if index == 0 else RepoTools(
                    root,
                    snapshot.base_sha,
                    snapshot.head_sha,
                    ticket_path=ticket_path,
                    ci_path=ci_path,
                    max_calls=budget.max_tool_calls_per_perspective,
                    cache=evidence_cache,
                )
                return review_diff(
                    llm=llm,
                    repo=root,
                    snapshot=snapshot,
                    tracker=tracker,
                    model=reviewer_model,
                    tool_host=tools,
                    sources=sources,
                    prompt=prompt,
                    allowed=allowed,
                    facts=facts,
                )

            with ThreadPoolExecutor(max_workers=max(1, len(passes))) as pool:
                results = list(pool.map(run_pass, range(len(passes))))
            findings, dropped, diffs, packs = [], [], {}, {}
            for found, lost, pass_diffs, pass_packs in results:
                findings += found
                dropped += lost
                diffs = pass_diffs
                packs.update(pass_packs)
            findings, gated = gate_simplifications(
                findings,
                policy=policy,
                repo=root,
                head_sha=snapshot.head_sha,
                sources=sources,
            )
            dropped = dropped + gated
            pending = [finding for finding in findings if finding.verification is None]
            held = [finding for finding in findings if finding.verification is not None]
            if pending and tracker.allow_model_call():
                pending = verify_findings(
                    llm=llm,
                    findings=pending,
                    tracker=tracker,
                    model=verifier_name,
                    sources=sources,
                    diffs=diffs,
                    packs=packs,
                    base_sha=snapshot.base_sha,
                    head_sha=snapshot.head_sha,
                    repo=Path(repo),
                    policy=policy if policy.active else None,
                    facts=facts,
                )
            elif pending:
                reason = tracker.reasons[-1] if tracker.reasons else "预算用尽，未完成验证"
                pending = [mark_undecided(finding, reason) for finding in pending]
            findings = held + pending

    if policy.active and policy.secrets_enabled and snapshot.reviewed:
        findings = scan_added_secrets(root, snapshot) + findings
    findings = _apply_levels(findings, policy)

    confirmed = []
    rejected = []
    undecided = []
    for finding in findings:
        verdict = (
            finding.verification.verdict if finding.verification else Verdict.undecided
        )
        if verdict == Verdict.confirmed:
            confirmed.append(finding)
        elif verdict == Verdict.rejected:
            rejected.append(finding)
        else:
            undecided.append(finding)

    stats = complexity_stats(root, snapshot) if policy.active and snapshot.reviewed else None
    if stats is not None:
        stats.exceeded = over_budget(stats, policy)
    fail_reasons = _fail_reasons(policy, confirmed, stats.exceeded if stats else [])

    reasons = list(snapshot.partial_reasons)
    for reason in tracker.reasons:
        if reason not in reasons:
            reasons.append(reason)
    status = _decide_status(
        tracker=tracker,
        snapshot_partial=snapshot.partial,
        finding_count=len(findings),
    )
    report = RunReport(
        status=status,
        base_sha=base_sha,
        head_sha=head_sha,
        prompt_version=PROMPT_VERSION,
        budget=budget,
        reuse_key=key,
        reviewer_model=reviewer_model,
        verifier_model=verifier_name,
        reviewed=snapshot.reviewed,
        unreviewed=snapshot.unreviewed,
        coverage=coverage,
        confirmed=confirmed,
        rejected=rejected,
        undecided=undecided,
        summary=_summary(undecided),
        elapsed_seconds=round(active_clock.elapsed(), 3),
        tokens=tracker.tokens,
        cost=tracker.cost,
        budget_exhausted=tracker.exhausted,
        partial_reasons=reasons,
        dropped=dropped,
        error=tracker.error,
        reused=False,
        policy_source=policy.source if policy.active else "default",
        policy_note=policy.note(),
        fail_on_must_fix=policy.fail_on_must_fix if policy.active else False,
        fail_check=bool(fail_reasons),
        fail_reasons=fail_reasons,
        complexity=stats,
    )
    _write_report(report, destination)
    return report
