"""Sub-rules, path overrides, the must-fix severity floor, and pre-existing findings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from pairrot.publish_github import plan_publication
from pairrot.review import prompt_for, validate_candidate
from tests.conftest import commit, init_repo, write


def _finding(
    category: Category,
    *,
    rule: str = "",
    file: str = "app.py",
    severity: Severity = Severity.S2,
    introduced: IntroducedIn = IntroducedIn.this_change,
) -> Finding:
    return Finding(
        fingerprint=f"{category.value}-{rule}-{file}",
        category=category,
        severity=severity,
        location=Location(file=file, start_line=1, end_line=1),
        title="t",
        trigger="t",
        rule=rule,
        introduced_in=introduced,
    )


def _completion(payload: dict) -> Completion:
    return Completion(
        message=ChatMessage(role="assistant", content=json.dumps(payload, ensure_ascii=False)),
        prompt_tokens=1,
        completion_tokens=1,
    )


def test_rules_grade_or_switch_off_one_rule_and_split_correctness():
    policy = policy_from_toml(
        """
[correctness]
level = "must_fix"
rules = { error_handling = "off" }

[security]
enabled = true
level = "must_fix"
rules = { authz = "recommend", timing_compare = "off" }
"""
    )
    assert Category.logic_error in policy.llm_categories()
    assert Category.error_handling not in policy.llm_categories()
    assert policy.decide(_finding(Category.logic_error, rule="logic_error")) == "must_fix"
    assert policy.decide(_finding(Category.error_handling, rule="error_handling")) == "off"
    assert policy.decide(_finding(Category.security, rule="injection")) == "must_fix"
    assert policy.decide(_finding(Category.security, rule="authz")) == "recommend"
    assert policy.decide(_finding(Category.security, rule="timing_compare")) == "off"
    assert "rules=" in policy.note()


def test_a_table_with_every_rule_off_is_not_sent_to_the_model():
    policy = policy_from_toml(
        '[performance]\nenabled = true\nrules = { n_plus_one = "off", quadratic = "off", repeated_io = "off", other = "off" }\n'
    )
    assert Category.performance not in policy.llm_categories()


def test_overrides_apply_by_path_in_order():
    policy = policy_from_toml(
        """
must_fix_min_severity = "S2"

[secrets]
enabled = true

[security]
enabled = true

[[override]]
paths = ["tests/**", "fixtures/*"]
off = ["secrets", "security.authz"]

[[override]]
paths = ["scripts/**"]
level = "recommend"

[[override]]
paths = ["core/**"]
must_fix_min_severity = "S3"
"""
    )
    assert policy.decide(_finding(Category.security, rule="token", file="tests/a/b.py"), secret=True) == "off"
    assert policy.decide(_finding(Category.security, rule="token", file="app.py"), secret=True) == "must_fix"
    assert policy.decide(_finding(Category.security, rule="authz", file="tests/x.py")) == "off"
    assert policy.decide(_finding(Category.security, rule="injection", file="tests/x.py")) == "must_fix"
    assert policy.decide(_finding(Category.security, rule="injection", file="scripts/run.py")) == "recommend"
    assert policy.decide(_finding(Category.security, rule="injection", severity=Severity.S3)) == "recommend"
    assert policy.decide(_finding(Category.security, rule="injection", severity=Severity.S3, file="core/a.py")) == "must_fix"


def test_preexisting_findings_stay_out_of_must_fix_unless_kept():
    old = _finding(Category.logic_error, rule="logic_error", severity=Severity.S1, introduced=IntroducedIn.preexisting)
    base = '[correctness]\nlevel = "must_fix"\n'
    assert policy_from_toml(base).decide(old) == "recommend"
    assert policy_from_toml('preexisting = "off"\n' + base).decide(old) == "off"
    assert policy_from_toml('preexisting = "keep"\n' + base).decide(old) == "must_fix"


@pytest.mark.parametrize(
    "text",
    [
        '[security]\nrules = { sqli = "off" }\n',
        '[security]\nrules = { injection = "block" }\n',
        '[[override]]\npaths = ["a/*"]\noff = ["security.sqli"]\n',
        '[[override]]\npaths = ["a/*"]\nlevels = "recommend"\n',
        '[[override]]\noff = ["secrets"]\n',
        'must_fix_min_severity = "S4"\n',
        'preexisting = "hide"\n',
    ],
)
def test_bad_rule_settings_are_rejected(text: str):
    with pytest.raises(PolicyError):
        policy_from_toml(text)


def test_digest_changes_with_rules():
    base = "[security]\nenabled = true\n"
    assert policy_from_toml(base).digest() != policy_from_toml(base + 'rules = { authz = "off" }\n').digest()


def test_prompt_lists_live_rules_and_names_the_ones_off():
    policy = policy_from_toml('[security]\nenabled = true\nrules = { timing_compare = "off", other = "off" }\n')
    prompt = prompt_for(policy)
    assert "rule 字段" in prompt and "injection（" in prompt
    assert "timing_compare（" not in prompt
    assert "这些不要报：用 == 比较令牌" in prompt


def test_reviewer_rule_tags_are_normalized():
    raw = {
        "category": "security",
        "severity": "S2",
        "file": "app.py",
        "start_line": 1,
        "end_line": 1,
        "trigger": "t",
        "rule": "sqli",
    }
    allowed = frozenset({Category.security, Category.logic_error})
    found, _ = validate_candidate(raw, reviewed_paths={"app.py"}, sources={}, allowed=allowed)
    assert found.rule == "other"
    found, _ = validate_candidate({**raw, "rule": "authz"}, reviewed_paths={"app.py"}, sources={}, allowed=allowed)
    assert found.rule == "authz"
    found, _ = validate_candidate({**raw, "category": "logic_error"}, reviewed_paths={"app.py"}, sources={}, allowed=allowed)
    assert found.rule == "logic_error"


def test_rules_off_are_dropped_before_the_verifier_and_secrets_follow_overrides(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(
        repo,
        "REVIEW.toml",
        """
[secrets]
enabled = true

[security]
enabled = true
rules = { authz = "off" }

[[override]]
paths = ["tests/**"]
off = ["secrets"]
""",
    )
    write(repo, "app.py", "x = 1\n")
    base = commit(repo, "base")
    write(repo, "app.py", "def get(user, order):\n    return order\n")
    write(repo, "tests/test_app.py", 'api_key = "supersecretvalue"\n')
    tip = commit(repo, "head")
    calls = {"verify": 0}

    def handler(messages, model):
        system = messages[0].content or ""
        if "独立验证" in system:
            calls["verify"] += 1
            return _completion({"verdict": "confirmed", "reason": "成立", "checked": [], "introduced_in": "this_change"})
        if "security" not in system:
            return _completion({"findings": []})
        return _completion(
            {
                "findings": [
                    {
                        "category": "security",
                        "rule": "authz",
                        "severity": "S1",
                        "file": "app.py",
                        "start_line": 2,
                        "end_line": 2,
                        "title": "没有归属校验",
                        "trigger": "任何用户可读别人的订单",
                        "snippet": "return order",
                        "introduced_in": "this_change",
                    }
                ]
            }
        )

    report = review_change(repo, base, tip, tmp_path / "out", llm=FakeModel(handler))
    assert report.confirmed == []
    assert calls["verify"] == 0
    reasons = [(item.reason, item.file) for item in report.dropped]
    assert ("rule off in REVIEW.toml", "app.py") in reasons
    assert ("rule off in REVIEW.toml", "tests/test_app.py") in reasons


def test_without_policy_preexisting_findings_stay_out_of_inline_comments():
    old = _finding(Category.logic_error, severity=Severity.S1, introduced=IntroducedIn.preexisting).model_copy(
        update={"verification": Verification(verdict=Verdict.confirmed, checked=[], reason="成立")}
    )
    report = RunReport(
        status=RunStatus.passed,
        base_sha="a" * 40,
        head_sha="b" * 40,
        prompt_version="review-v13",
        budget=Budget(),
        coverage=Coverage(ci="无", ticket="无"),
        confirmed=[old],
    )
    planned = plan_publication(report, {"app.py": {1}}, set())
    assert planned.inline == []
    assert "改动前已存在" in planned.summary
