"""Base REVIEW.toml, secret scanning, and must-fix versus recommend."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pairrot.cli import exit_code, load_dotenv, main
from pairrot.policy import policy_from_toml
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
from pairrot.publish_github import plan_publication
from pairrot.redact import secret_name
from tests.conftest import commit, init_repo, write

_SECRET = 'api_key = "supersecretvalue"\n'


def _completion(payload: dict) -> Completion:
    return Completion(
        message=ChatMessage(role="assistant", content=json.dumps(payload, ensure_ascii=False)),
        prompt_tokens=1,
        completion_tokens=1,
    )


def _quiet() -> FakeModel:
    return FakeModel(lambda messages, model: _completion({"findings": []}))


def _repo(tmp_path: Path, toml: str | None, head: str, *, toml_on_base: bool) -> tuple[Path, str, str]:
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "def clamp(value, low, high):\n    return value\n")
    if toml_on_base and toml is not None:
        write(repo, "REVIEW.toml", toml)
    base = commit(repo, "base")
    write(repo, "app.py", head)
    if not toml_on_base and toml is not None:
        write(repo, "REVIEW.toml", toml)
    tip = commit(repo, "head")
    return repo, base, tip


def test_secret_name_ignores_ordinary_code():
    assert secret_name(_SECRET) == "api_key"
    assert secret_name("def clamp(value, low, high):\n") is None
    assert secret_name('password = "x"') is None


def test_without_policy_a_secret_is_not_a_finding(tmp_path: Path):
    repo, base, tip = _repo(tmp_path, None, _SECRET, toml_on_base=True)
    report = review_change(repo, base, tip, tmp_path / "out", llm=_quiet())
    assert report.policy_source == "default"
    assert report.confirmed == []
    assert "supersecretvalue" not in (tmp_path / "out" / "report.json").read_text(encoding="utf-8")


def test_base_policy_reports_a_secret_without_the_value_and_can_fail_the_check(tmp_path: Path):
    toml = "fail_on_must_fix = true\n\n[secrets]\nenabled = true\nlevel = \"must_fix\"\n"
    repo, base, tip = _repo(tmp_path, toml, "def clamp(value, low, high):\n    return min(max(value, low), high)\n" + _SECRET, toml_on_base=True)
    model = _quiet()
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    assert report.status.value == "passed"
    assert report.fail_check
    assert exit_code(report) == 1
    assert [item.category for item in report.confirmed] == [Category.security]
    assert report.confirmed[0].level == "must_fix"
    assert report.confirmed[0].title == "新增了疑似密钥"
    blob = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    assert "supersecretvalue" not in blob
    planned = plan_publication(report, {"app.py": {report.confirmed[0].location.start_line}}, set())
    assert planned.inline
    assert "必须处理" in planned.inline[0].body
    assert "supersecretvalue" not in planned.inline[0].body
    assert "supersecretvalue" not in planned.summary
    assert "### 必须处理" in planned.summary
    assert "失败：存在必须处理的意见" in planned.summary
    assert "clamp" not in planned.summary
    assert len(model.calls) == 1


def test_policy_on_the_head_commit_does_not_apply(tmp_path: Path):
    toml = "[secrets]\nenabled = true\nlevel = \"must_fix\"\n"
    repo, base, tip = _repo(tmp_path, toml, _SECRET, toml_on_base=False)
    report = review_change(repo, base, tip, tmp_path / "out", llm=_quiet())
    assert report.policy_source == "default"
    assert report.confirmed == []


def test_disabling_correctness_skips_the_model(tmp_path: Path):
    toml = "[correctness]\nenabled = false\n"
    repo, base, tip = _repo(tmp_path, toml, "def clamp(value, low, high):\n    return low\n", toml_on_base=True)

    def handler(messages, model):
        raise AssertionError("model should not be called")

    report = review_change(repo, base, tip, tmp_path / "out", llm=FakeModel(handler))
    assert report.confirmed == []
    assert report.status.value == "passed"
    assert exit_code(report) == 0


def test_performance_and_extra_rules_follow_the_base_file(tmp_path: Path):
    toml = """
[performance]
enabled = true
level = "recommend"

[[extra]]
enabled = true
instruction = "PROBE_RULE"
"""
    repo, base, tip = _repo(tmp_path, toml, "def work(rows):\n    return rows\n", toml_on_base=True)

    def handler(messages, model):
        system = messages[0].content or ""
        if "独立验证" in system:
            return _completion(
                {"verdict": "confirmed", "reason": "规模成立", "checked": ["path_reachable"], "introduced_in": "this_change"}
            )
        assert "输入规模" in system
        assert "PROBE_RULE" in system
        return _completion(
            {
                "findings": [
                    {
                        "category": "performance",
                        "severity": "S2",
                        "file": "app.py",
                        "start_line": 1,
                        "end_line": 1,
                        "title": "双重循环扫全表",
                        "trigger": "10 万行时内层再扫一遍",
                        "consequence": "请求变慢",
                        "suggestion": "先建索引",
                        "snippet": "return rows",
                        "introduced_in": "this_change",
                    },
                    {
                        "category": "security",
                        "severity": "S2",
                        "file": "app.py",
                        "start_line": 1,
                        "end_line": 1,
                        "title": "模型自己报的密钥",
                        "trigger": "模型猜测",
                        "consequence": "不该保留",
                        "suggestion": "丢掉",
                        "snippet": "return rows",
                        "introduced_in": "this_change",
                    },
                ]
            }
        )

    report = review_change(repo, base, tip, tmp_path / "out", llm=FakeModel(handler))
    assert [item.category for item in report.confirmed] == [Category.performance]
    assert report.confirmed[0].level == "recommend"
    assert {item.reason for item in report.dropped} == {"category not enabled"}
    planned = plan_publication(report, {"app.py": {1}}, set())
    assert planned.inline == []
    assert "### 建议" in planned.summary
    assert "双重循环扫全表" in planned.summary
    assert "模型自己报的密钥" not in planned.summary


def test_security_perspective_has_its_own_level_apart_from_secrets(tmp_path: Path):
    toml = """
[secrets]
enabled = true
level = "recommend"

[security]
enabled = true
level = "must_fix"
"""
    head = "def find(conn, name):\n    return conn.execute(f\"SELECT * FROM t WHERE name = '{name}'\")\n" + _SECRET
    repo, base, tip = _repo(tmp_path, toml, head, toml_on_base=True)

    def handler(messages, model):
        system = messages[0].content or ""
        if "独立验证" in system:
            assert "参数化" in system
            return _completion(
                {"verdict": "confirmed", "reason": "name 来自请求", "checked": ["path_reachable"], "introduced_in": "this_change"}
            )
        assert "security" in system and "利用路径" in system
        return _completion(
            {
                "findings": [
                    {
                        "category": "security",
                        "severity": "S1",
                        "file": "app.py",
                        "start_line": 2,
                        "end_line": 2,
                        "title": "name 拼进 SQL",
                        "trigger": "name = \"x' OR '1'='1\"",
                        "consequence": "返回整张表",
                        "suggestion": "改成参数化查询",
                        "snippet": "conn.execute",
                        "introduced_in": "this_change",
                    }
                ]
            }
        )

    report = review_change(repo, base, tip, tmp_path / "out", llm=FakeModel(handler))
    levels = {item.title: item.level for item in report.confirmed}
    assert levels == {"新增了疑似密钥": "recommend", "name 拼进 SQL": "must_fix"}
    assert "security=开启:must_fix" in report.policy_note


def test_broken_policy_fails_the_run(tmp_path: Path):
    repo, base, tip = _repo(tmp_path, "fail_on_must_fix = \n", "x = 1\n", toml_on_base=True)
    report = review_change(repo, base, tip, tmp_path / "out", llm=_quiet())
    assert report.status.value == "failed"
    assert report.error and "REVIEW.toml 无法解析" in report.error
    assert exit_code(report) == 1


def test_leveled_summary_keeps_recommend_off_the_diff(tmp_path: Path):
    must = Finding(
        fingerprint="must",
        category=Category.security,
        severity=Severity.S2,
        location=Location(file="app.py", start_line=2, end_line=2),
        title="新增了疑似密钥",
        trigger="新增赋值 api_key，值已隐藏。",
        consequence="明文密钥会进入仓库历史。",
        suggestion="改为环境变量。",
        verification=Verification(verdict=Verdict.confirmed, checked=["secret_pattern"], reason="匹配"),
        introduced_in=IntroducedIn.this_change,
        level="must_fix",
    )
    advise = Finding(
        fingerprint="advise",
        category=Category.logic_error,
        severity=Severity.S1,
        location=Location(file="app.py", start_line=4, end_line=4),
        title="边界少了一次",
        trigger="库存等于请求时走了拒绝分支。",
        consequence="本应分配的请求变成 0。",
        suggestion="改成大于等于。",
        verification=Verification(verdict=Verdict.confirmed, checked=[], reason="成立"),
        introduced_in=IntroducedIn.this_change,
        level="recommend",
    )
    report = RunReport(
        status=RunStatus.passed,
        base_sha="a" * 40,
        head_sha="b" * 40,
        prompt_version="phase1-correctness-v3",
        budget=Budget(),
        coverage=Coverage(ci="无", ticket="无"),
        confirmed=[must, advise],
        policy_source="base",
        policy_note="secrets=开启:must_fix",
        fail_on_must_fix=False,
    )
    planned = plan_publication(report, {"app.py": {2, 4}}, set())
    assert [item.fingerprint for item in planned.inline] == ["must"]
    assert "边界少了一次" in planned.summary
    assert "建议" in planned.summary
    assert "不因意见失败" in planned.summary


def test_a_policy_saved_with_a_bom_and_crlf_still_applies(tmp_path: Path):
    toml = "\ufeff[secrets]\r\nenabled = true\r\nlevel = \"must_fix\"\r\n"
    repo, base, tip = _repo(tmp_path, toml, _SECRET, toml_on_base=True)
    report = review_change(repo, base, tip, tmp_path / "out", llm=_quiet())
    assert report.policy_source == "base"
    assert [item.title for item in report.confirmed] == ["新增了疑似密钥"]


def test_a_lowercase_policy_file_fails_with_its_name(tmp_path: Path, capsys):
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "x = 1\n")
    write(repo, "review.toml", "[secrets]\nenabled = true\n")
    base = commit(repo, "base")
    write(repo, "app.py", _SECRET)
    tip = commit(repo, "head")
    code = main(["review", "--repo", str(repo), "--base", base, "--head", tip, "--out", str(tmp_path / "out")])
    assert code == 1
    assert "base 上是 review.toml" in capsys.readouterr().err


def test_init_writes_the_starter_policy_and_keeps_an_existing_one(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert main(["init", "--repo", str(repo)]) == 0
    written = (repo / "REVIEW.toml").read_bytes()
    assert b"\r\n" not in written and not written.startswith(b"\xef\xbb\xbf")
    assert policy_from_toml(written.decode("utf-8")).secrets_enabled
    (repo / "REVIEW.toml").write_text("fail_on_must_fix = true\n", encoding="utf-8")
    assert main(["init", "--repo", str(repo)]) == 1
    assert "已存在" in capsys.readouterr().err
    assert (repo / "REVIEW.toml").read_text(encoding="utf-8") == "fail_on_must_fix = true\n"
    assert main(["init", "--repo", str(repo), "--force"]) == 0
    assert (repo / "REVIEW.toml").read_bytes() == written


def test_review_says_when_the_policy_is_only_in_the_worktree(tmp_path: Path, capsys, monkeypatch):
    repo, base, tip = _repo(tmp_path, None, "def clamp(value, low, high):\n    return low\n", toml_on_base=True)
    (repo / "REVIEW.toml").write_text("[secrets]\nenabled = true\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("pairrot.cli.review_change", lambda *a, **k: review_change(*a, **k, llm=_quiet()))
    main(["review", "--repo", str(repo), "--base", base, "--head", tip, "--out", str(tmp_path / "out")])
    assert "base 上没有" in capsys.readouterr().err


def test_dotenv_saved_with_a_bom_keeps_its_first_key(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    env.write_bytes("\ufeffPAIRROT_TEST_KEY=value\r\n".encode("utf-8"))
    monkeypatch.delenv("PAIRROT_TEST_KEY", raising=False)
    monkeypatch.delenv("PAIRROT_DISABLE_DOTENV", raising=False)
    load_dotenv(env)
    assert os.environ.pop("PAIRROT_TEST_KEY") == "value"
