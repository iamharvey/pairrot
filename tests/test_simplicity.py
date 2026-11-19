"""Simplification findings, intentional incompatibility, the complexity budget, and the override label."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from pairrot.cli import exit_code
from pairrot.facts import HEADER, design_facts, facts_for
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
from pairrot.policy import PolicyError, policy_from_toml
from pairrot.publish_github import GitHubClient, plan_publication, publish_github
from pairrot.review import load_sources, prompt_for
from pairrot.simplify import gate_simplifications, imported_modules
from pairrot.snapshot import take_snapshot
from pairrot.verify import VERIFY_SYSTEM_PROMPT, _candidate_payload, _context
from tests.conftest import commit, init_repo, write

_BASE = '"""Word statistics."""\n\n\ndef words(text):\n    return text.split()\n'
_HEAD = (
    '"""Word statistics."""\n\n\n'
    "def words(text):\n"
    "    return text.split()\n\n\n"
    "def tally(items):\n"
    "    counts = {}\n"
    "    for item in items:\n"
    "        if item in counts:\n"
    "            counts[item] += 1\n"
    "        else:\n"
    "            counts[item] = 1\n"
    "    return counts\n\n\n"
    "def first_or_none(items):\n"
    "    for item in items:\n"
    "        return item\n"
    "    return None\n"
)
_TOML = """
[simplicity]
enabled = true
level = "must_fix"
extension_points = ["plugins/*"]
"""


def _completion(payload: dict) -> Completion:
    return Completion(
        message=ChatMessage(role="assistant", content=json.dumps(payload, ensure_ascii=False)),
        prompt_tokens=1,
        completion_tokens=1,
    )


def _repo(tmp_path: Path, toml: str) -> tuple[Path, str, str]:
    repo = init_repo(tmp_path / "repo")
    write(repo, "stats.py", _BASE)
    write(repo, "REVIEW.toml", toml)
    write(repo, "plugins/base.py", "class Exporter:\n    def export(self, rows):\n        raise NotImplementedError\n")
    base = commit(repo, "base")
    write(repo, "stats.py", _HEAD)
    write(repo, "tests/test_stats.py", "from stats import tally\n\n\ndef test_tally():\n    assert tally('aab') == {'a': 2, 'b': 1}\n")
    write(repo, "plugins/base.py", "class Exporter:\n    def export(self, rows):\n        raise NotImplementedError\n\n\nclass Csv(Exporter):\n    pass\n")
    tip = commit(repo, "head")
    return repo, base, tip


def _simplification(**overrides) -> dict:
    item = {
        "category": "complexity",
        "severity": "S3",
        "file": "stats.py",
        "start_line": 9,
        "end_line": 15,
        "title": "手写计数可以换成 Counter",
        "trigger": "tally 手写字典计数",
        "consequence": "多出七行需要维护的代码",
        "suggestion": "用 collections.Counter",
        "snippet": "counts = {}",
        "introduced_in": "this_change",
        "replacement": "    return dict(collections.Counter(items))",
        "equivalence": "空输入返回空字典，键顺序按首次出现，异常类型相同",
        "performance": "同为 O(n)，没有额外 IO",
    }
    item.update(overrides)
    return item


def test_policy_reads_compatibility_simplicity_and_budget_tables():
    policy = policy_from_toml(
        """
[compatibility]
enabled = true
stable = ["api/*", "proto/*"]
allow_break = ["api/v1_orders.py"]

[simplicity]
enabled = true
extension_points = ["plugins/*"]

[complexity_budget]
max_single_use = 3
max_net_added = 400
"""
    )
    assert Category.contract_break in policy.llm_categories()
    assert Category.complexity in policy.llm_categories()
    assert policy.is_stable("api/users.py")
    assert not policy.is_stable("api/v1_orders.py")
    assert not policy.is_stable("app/users.py")
    assert dict(policy.budget_limits) == {"max_single_use": 3, "max_net_added": 400}
    def sample(category: Category) -> Finding:
        return Finding(
            fingerprint="x",
            category=category,
            severity=Severity.S2,
            location=Location(file="app.py", start_line=1, end_line=1),
            trigger="t",
        )

    assert policy.decide(sample(Category.complexity), locked=True) == "must_fix"
    assert policy.decide(sample(Category.complexity), locked=False) == "recommend"
    assert policy.decide(sample(Category.contract_break)) == "must_fix"
    with pytest.raises(PolicyError):
        policy_from_toml("[complexity_budget]\nmax_lines = 3\n")
    with pytest.raises(PolicyError):
        policy_from_toml("[compatibility]\nstable = 'api/*'\n")


def test_prompt_forbids_compatibility_shims_and_names_stable_paths():
    plain = prompt_for(policy_from_toml("[correctness]\nenabled = true\n"))
    assert "suggestion 不得提议增加兼容层" in plain
    assert "contract_break" not in plain
    policy = policy_from_toml('[compatibility]\nenabled = true\nstable = ["api/*"]\nallow_break = ["api/old.py"]\n')
    main = prompt_for(policy)
    design = prompt_for(policy, design=True)
    assert "suggestion 不得提议增加兼容层" in main and "contract_break" not in main
    assert "稳定接口路径：api/*" in design
    assert "api/old.py" in design
    assert "仍有调用方或读取方" in design
    assert policy.main_categories() == {Category.logic_error, Category.error_handling}
    assert policy.design_categories() == {Category.contract_break}


def test_verifier_is_told_whether_the_path_is_a_stable_contract():
    policy = policy_from_toml('[compatibility]\nenabled = true\nstable = ["api/*"]\nallow_break = ["api/old.py"]\n')

    def payload(path: str) -> dict:
        finding = Finding(
            fingerprint="c",
            category=Category.contract_break,
            severity=Severity.S2,
            location=Location(file=path, start_line=1, end_line=1),
            trigger="字段改名",
        )
        return json.loads(_candidate_payload(finding, policy))

    assert payload("api/users.py")["stable_contract"] is True
    assert payload("api/old.py")["stable_contract"] is False
    assert payload("app/users.py")["stable_contract"] is False
    assert "仓库内已无旧用法" in VERIFY_SYSTEM_PROMPT


def test_verifier_sees_the_facts_for_design_candidates_only():
    facts = "下面是 pairrot 用 git 统计的事实，不是推断。\n- app/a.py helper：0 处\n- app/b.py other：2 处"

    def context(category: Category) -> str:
        finding = Finding(
            fingerprint="f",
            category=category,
            severity=Severity.S3,
            location=Location(file="app/a.py", start_line=1, end_line=1),
            trigger="t",
        )
        return _context(finding, {"app/a.py": "def helper():\n    pass\n"}, {}, [], base_sha="a", head_sha="b", facts=facts)

    assert "app/a.py helper：0 处" in context(Category.complexity)
    assert "app/b.py other" not in context(Category.complexity)
    assert "helper：0 处" not in context(Category.logic_error)


def test_facts_for_matches_the_path_not_a_substring():
    facts = "\n".join(
        [
            HEADER,
            "- api.py helper：0 处",
            "- rest_api.py other：1 处（rest_api.py:3）",
            "- app/api.py nested：2 处",
            "- api.pyi stub：0 处",
            "- fmt.py subject：旧 `def subject(n):`，新 `def subject(n, locale):`。head 全仓调用点：api.py:7: subject(x)",
        ]
    )
    related = facts_for(facts, "api.py")
    assert "helper" in related and "subject" in related
    assert "rest_api" not in related and "nested" not in related and "stub" not in related


def test_a_pure_deletion_on_the_diff_suggests_an_empty_block():
    finding = Finding(
        fingerprint="d",
        category=Category.complexity,
        severity=Severity.S3,
        location=Location(file="a.py", start_line=3, end_line=4),
        title="没有调用方的函数",
        trigger="unused 没有调用方",
        replacement="",
        equivalence="没有调用方，删除不改变任何行为",
        performance_note="少一个函数",
        verification=Verification(verdict=Verdict.confirmed, checked=[], reason="成立"),
        introduced_in=IntroducedIn.this_change,
        level="must_fix",
    )
    [comment] = plan_publication(_report([finding]), {"a.py": {3, 4}}, set()).inline
    assert (comment.start_line, comment.line) == (3, 4)
    assert "```suggestion\n```" in comment.body


def test_imported_modules_reads_plain_and_from_imports():
    code = "import collections, os.path as p\nfrom itertools import groupby\nfrom . import local\nimport requests\n"
    assert imported_modules(code) == {"collections", "os", "itertools", "requests"}


def test_simplifications_are_gated_locked_and_leveled(tmp_path: Path):
    repo, base, tip = _repo(tmp_path, _TOML)
    verified: list[str] = []

    def handler(messages, model):
        system = messages[0].content or ""
        if "独立验证" in system:
            payload = messages[1].content or ""
            assert "三道关" in system
            assert "replacement" in payload and "equivalence" in payload
            verified.append(payload)
            return _completion({"verdict": "confirmed", "reason": "等价", "checked": ["path_reachable"], "introduced_in": "this_change"})
        if "这一轮只看设计" not in system:
            assert "complexity" not in system
            return _completion({"findings": []})
        assert "complexity 只报本次变更引入的" in system
        assert "不得引入新的第三方依赖" in system
        assert "每条发现只描述一个失败" not in system
        return _completion(
            {
                "findings": [
                    _simplification(),
                    _simplification(
                        start_line=19,
                        end_line=21,
                        title="手写取首项可以换成 next",
                        snippet="for item in items:",
                        replacement="    return next(iter(items), None)",
                    ),
                    _simplification(
                        title="换成 requests 的工具函数",
                        replacement="    import requests\n    return requests.utils.dict_from_cookiejar(items)",
                        snippet="counts[item] += 1",
                        start_line=12,
                        end_line=12,
                    ),
                    _simplification(title="没写等价说明", equivalence="", snippet="counts[item] = 1", start_line=14, end_line=14),
                    _simplification(
                        title="tally 和 words 合并",
                        snippet="def tally(items):",
                        start_line=8,
                        end_line=8,
                        replacement=None,
                        edits=["stats.py:8 删掉 tally", "tests/test_stats.py:1 改成 Counter"],
                    ),
                    _simplification(title="只有一处改动却不给替换", snippet="return counts", start_line=15, end_line=15, replacement=None, edits=["stats.py:15 删掉"]),
                    _simplification(
                        file="plugins/base.py",
                        start_line=6,
                        end_line=7,
                        title="只有一个实现的基类",
                        snippet="class Csv(Exporter):",
                        replacement="",
                    ),
                ]
            }
        )

    report = review_change(repo, base, tip, tmp_path / "out", llm=FakeModel(handler))
    confirmed = {item.title: item for item in report.confirmed}
    assert set(confirmed) == {"手写计数可以换成 Counter", "手写取首项可以换成 next", "tally 和 words 合并"}
    merged = confirmed["tally 和 words 合并"]
    assert merged.locked_by and merged.level == "recommend"
    assert merged.edits == ["stats.py:8 删掉 tally", "tests/test_stats.py:1 改成 Counter"]
    counter = confirmed["手写计数可以换成 Counter"]
    assert counter.locked_by == ["tests/test_stats.py"]
    assert counter.level == "must_fix"
    unlocked = confirmed["手写取首项可以换成 next"]
    assert unlocked.locked_by == []
    assert unlocked.level == "recommend"
    rejected = {item.title: item.verification.reason for item in report.rejected}
    assert "requests" in rejected["换成 requests 的工具函数"]
    assert len(verified) == 3
    reasons = [item.reason for item in report.dropped]
    assert reasons.count("simplification missing replacement or edits, equivalence, or performance") == 2
    assert "extension point" in reasons

    planned = plan_publication(report, {"stats.py": set(range(1, 22))}, set())
    [comment] = planned.inline
    assert comment.start_line == 9 and comment.line == 15
    assert "```suggestion\n    return dict(collections.Counter(items))\n```" in comment.body
    assert "等价：" in comment.body and "性能：" in comment.body
    assert "`tests/test_stats.py`" in comment.body
    assert "无测试锁定，采纳前先补测试" in planned.summary
    assert "需要一起改：stats.py:8 删掉 tally；tests/test_stats.py:1 改成 Counter" in planned.summary
    assert "### 复杂度统计" in planned.summary


def test_simplification_off_the_diff_gets_a_plain_code_block():
    finding = Finding(
        fingerprint="s",
        category=Category.complexity,
        severity=Severity.S3,
        location=Location(file="a.py", start_line=3, end_line=5),
        title="只转发的包装层",
        trigger="wrap 只调用 inner",
        replacement="",
        equivalence="调用方直接用 inner，参数和返回值相同",
        performance_note="少一次函数调用",
        verification=Verification(verdict=Verdict.confirmed, checked=[], reason="成立"),
        introduced_in=IntroducedIn.this_change,
        level="must_fix",
    )
    report = _report([finding])
    [comment] = plan_publication(report, {"a.py": {5}}, set()).inline
    assert comment.start_line is None and comment.line == 5
    assert "```suggestion" not in comment.body
    assert "替换为：删除这段代码" in comment.body
    assert "锁定测试：无" in comment.body


def test_budget_over_its_limit_fails_the_check_without_fail_on_must_fix(tmp_path: Path):
    toml = "[correctness]\nenabled = false\n\n[complexity_budget]\nmax_single_use = 1\nmax_net_added = 100\n"
    repo, base, tip = _repo(tmp_path, toml)
    report = review_change(repo, base, tip, tmp_path / "out", llm=FakeModel(lambda *a: _completion({"findings": []})))
    stats = report.complexity
    assert stats is not None
    assert set(stats.new_symbols) == {"tally", "first_or_none", "Csv"}
    assert set(stats.single_use) == {"tally", "first_or_none", "Csv"}
    assert stats.exceeded == ["全仓只用一次或没用到的新符号 3，上限 1"]
    assert report.fail_check
    assert report.fail_reasons == ["复杂度预算超限：全仓只用一次或没用到的新符号 3，上限 1"]
    assert exit_code(report) == 1
    assert exit_code(report, defer_check=True) == 0
    planned = plan_publication(report, {}, set())
    assert planned.check == "failed"
    assert "失败：复杂度预算超限" in planned.summary


def test_design_facts_list_stale_callers_reference_counts_and_duplicates(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "util.py", 'def clean(text):\n    """Trim and lower."""\n    return text.strip().lower()\n')
    write(repo, "fmt.py", "def subject(n):\n    return n.title\n")
    write(repo, "digest.py", "from fmt import subject\n\n\ndef lines(items):\n    return [subject(n) for n in items]\n")
    base = commit(repo, "base")
    write(repo, "fmt.py", "def subject(n, locale):\n    return n.title\n")
    write(
        repo,
        "api.py",
        '"""We clean_email before handling."""\n\n\n'
        "def clean_email(text):\n    return text.strip().lower()\n\n\n"
        "def handle(body):\n    return clean_email(body)\n\n\nsend_mail = handle\n",
    )
    write(repo, "flags.py", "USE_V2 = True\n\n\ndef pick(n):\n    if USE_V2:\n        return n\n    return -n\n")
    tip = commit(repo, "head")
    snapshot = take_snapshot(repo, base, tip)
    facts = design_facts(repo, snapshot, load_sources(repo, snapshot))
    assert "fmt.py subject：旧 `def subject(n):`，新 `def subject(n, locale):`" in facts
    assert "digest.py:5: return [subject(n) for n in items]" in facts
    assert "api.py clean_email：1 处（api.py:9）" in facts
    assert "api.py handle：1 处（api.py:12）" in facts
    assert "api.py send_mail：0 处" in facts
    assert "api.py clean_email 的实现与 util.py:1 clean 完全相同" in facts
    assert "flags.py USE_V2 = True：head 全仓赋值 1 处（flags.py:1），读取处：flags.py:5" in facts
    related = facts_for(facts, "api.py")
    assert "clean_email" in related and "USE_V2" not in related and "digest.py" not in related


def test_splice_gate_widens_whole_function_rewrites_and_rejects_broken_ones(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "def keep():\n    return 1\n")
    base = commit(repo, "base")
    source = (
        "def keep():\n    return 1\n\n\n"
        "def dedupe(ids):\n    seen = []\n    for item in ids:\n        if item not in seen:\n            seen.append(item)\n    return seen\n\n\n"
        "def clean(text):\n    return text.strip()\n\n\n"
        "def handle(body):\n    return clean(body)\n"
    )
    write(repo, "app.py", source)
    write(repo, "other.py", "from app import keep\n\nVALUE = keep()\n")
    tip = commit(repo, "head")
    sources = {"app.py": source}
    policy = policy_from_toml(_TOML)

    def gate(start: int, end: int, replacement: str) -> Finding:
        finding = Finding.model_validate(
            {
                "fingerprint": f"g{start}",
                "category": "complexity",
                "severity": "S3",
                "location": {"file": "app.py", "start_line": start, "end_line": end},
                "trigger": "t",
                "replacement": replacement,
            }
        )
        (kept,), _ = gate_simplifications([finding], policy=policy, repo=repo, head_sha=tip, sources=sources)
        return kept

    whole = gate(5, 8, "def dedupe(ids):\n    return list(dict.fromkeys(ids))")
    assert (whole.location.start_line, whole.location.end_line) == (5, 10)
    assert whole.verification is None
    signature = gate(5, 5, "def dedupe(items, *, strict=False):")
    assert (signature.location.start_line, signature.location.end_line) == (5, 5)
    assert signature.verification is None
    assert "无法解析" in gate(6, 6, "seen = []").verification.reason
    assert "仍在使用" in gate(13, 14, "").verification.reason
    assert "other.py:1" in gate(1, 2, "").verification.reason


def _report(confirmed: list[Finding], *, fail: bool = False) -> RunReport:
    return RunReport(
        status=RunStatus.passed,
        base_sha="a" * 40,
        head_sha="b" * 40,
        prompt_version="phase1-correctness-v11",
        budget=Budget(),
        coverage=Coverage(ci="无", ticket="无"),
        confirmed=confirmed,
        policy_source="base",
        fail_on_must_fix=True,
        fail_check=fail,
        fail_reasons=["存在必须处理的意见"] if fail else [],
    )


def _github(labels: list[str], calls: list[tuple[str, str]]) -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/labels") and request.method == "GET":
            return httpx.Response(200, json=[{"name": name} for name in labels])
        if path.endswith("/events"):
            return httpx.Response(
                200,
                json=[
                    {"event": "labeled", "label": {"name": "pairrot-override"}, "actor": {"login": "first"}},
                    {"event": "labeled", "label": {"name": "other"}, "actor": {"login": "noise"}},
                    {"event": "labeled", "label": {"name": "pairrot-override"}, "actor": {"login": "lead"}},
                ],
            )
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"id": 1})

    return GitHubClient("token", "acme/pairrot", transport=httpx.MockTransport(handler))


def test_override_label_passes_the_check_and_names_who_applied_it():
    calls: list[tuple[str, str]] = []
    planned = publish_github(_report([], fail=True), 7, client=_github(["pairrot-override"], calls))
    assert planned.check == "overridden"
    assert "已人工放行：存在必须处理的意见。@lead" in planned.summary


def test_new_commits_remove_the_override_label():
    calls: list[tuple[str, str]] = []
    planned = publish_github(_report([], fail=True), 7, client=_github(["pairrot-override"], calls), new_commits=True)
    assert planned.check == "failed"
    assert ("DELETE", "/repos/acme/pairrot/issues/7/labels/pairrot-override") in calls
    assert "标签已撤销" in planned.summary


def test_without_the_label_a_must_fix_fails_the_check():
    calls: list[tuple[str, str]] = []
    planned = publish_github(_report([], fail=True), 7, client=_github([], calls))
    assert planned.check == "failed"
    assert not any(method == "DELETE" for method, _ in calls)
    assert publish_github(_report([]), 7, client=_github([], calls)).check == "ok"
