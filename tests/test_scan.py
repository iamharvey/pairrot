"""Resumable full-tree scan and the baseline later reviews skip."""

from __future__ import annotations

import json
from pathlib import Path

from pairrot.baseline import load_baseline, save_baseline, without_baseline
from pairrot.llm import ChatMessage, Completion, FakeModel
from pairrot.models import Budget
from pairrot.pipeline import review_change
from pairrot.policy import Policy
from pairrot.review import audit_prompt
from pairrot.scan import list_files, scan_tree
from pairrot.secrets_scan import secret_finding
from pairrot.verify import AUDIT_VERIFY_PROMPT
from tests.conftest import commit, init_repo, write


def _file_of(user: str) -> str:
    for line in user.splitlines():
        if line.startswith("file "):
            return line.removeprefix("file ").strip()
    return "unknown"


def _model(calls: list[str], *, cost: float = 1) -> FakeModel:
    def handler(messages, model):
        system = messages[0].content or ""
        user = messages[-1].content or ""
        if "独立验证" in system:
            assert "不要因为问题早就存在" in system
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=json.dumps(
                        {"verdict": "confirmed", "reason": "成立", "checked": ["path_reachable"], "introduced_in": "preexisting"}
                    ),
                ),
                prompt_tokens=1,
                completion_tokens=1,
                cost=cost,
            )
        name = _file_of(user)
        calls.append(name)
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=json.dumps(
                    {
                        "findings": [
                            {
                                "category": "logic_error",
                                "severity": "S1",
                                "file": name,
                                "start_line": 2,
                                "end_line": 2,
                                "title": f"{name} 直接返回",
                                "trigger": "任何输入都原样返回",
                                "consequence": "下界没有生效",
                                "suggestion": "先和 low 比较",
                                "snippet": "return value",
                                "introduced_in": "this_change",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            ),
            prompt_tokens=1,
            completion_tokens=1,
            cost=cost,
        )

    return FakeModel(handler)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = init_repo(tmp_path / "repo")
    write(repo, "a.py", "def clamp(value):\n    return value\n")
    write(repo, "b.py", "def clamp(value):\n    return value\n")
    write(repo, "src/c.py", "x = 1\n")
    return repo, commit(repo, "base")


def test_scan_resumes_and_does_not_repeat_a_finished_file(tmp_path: Path):
    repo, rev = _repo(tmp_path)
    calls: list[str] = []
    budget = Budget(max_files=1, cost_cap=None)
    first = scan_tree(repo, rev, tmp_path / "out", llm=_model(calls), budget=budget)
    assert first.status.value == "partial"
    assert [item.path for item in first.reviewed] == ["a.py"]
    assert any(item.path == "b.py" and item.reason == "尚未体检" for item in first.unreviewed)
    assert [item.title for item in first.confirmed] == ["a.py 直接返回"]
    second = scan_tree(repo, rev, tmp_path / "out", llm=_model(calls), budget=budget)
    assert [item.path for item in second.reviewed] == ["a.py", "b.py"]
    assert calls == ["a.py", "b.py"]
    assert "全量体检" in second.policy_note or second.head_kind == "scan"


def test_a_cost_cap_stops_before_the_file_counts_as_done(tmp_path: Path):
    repo, rev = _repo(tmp_path)
    calls: list[str] = []
    report = scan_tree(repo, rev, tmp_path / "out", llm=_model(calls), budget=Budget(cost_cap=1))
    assert report.reviewed == []
    assert report.budget_exhausted
    assert any(item.reason == "尚未体检" for item in report.unreviewed)


def test_baseline_is_saved_only_when_the_scan_finishes_and_review_skips_it(tmp_path: Path):
    repo, rev = _repo(tmp_path)
    calls: list[str] = []
    early = scan_tree(
        repo,
        rev,
        tmp_path / "out",
        llm=_model(calls),
        budget=Budget(max_files=1),
        save_baseline_to=tmp_path / "base.json",
    )
    assert "不能写基线" in (early.error or "")
    assert not (tmp_path / "base.json").exists()
    done = scan_tree(
        repo,
        rev,
        tmp_path / "out",
        llm=_model(calls),
        budget=Budget(max_files=10),
        save_baseline_to=tmp_path / "base.json",
    )
    assert done.error is None
    assert {item.location.file for item in done.confirmed} == {"a.py", "b.py", "src/c.py"}
    saved = load_baseline(tmp_path / "base.json")
    assert saved == {item.fingerprint for item in done.confirmed}

    def review_model(messages, model):
        system = messages[0].content or ""
        if "独立验证" in system:
            return Completion(
                message=ChatMessage(role="assistant", content=json.dumps({"verdict": "confirmed", "reason": "成立", "checked": []})),
                prompt_tokens=1,
                completion_tokens=1,
            )
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=json.dumps(
                    {
                        "findings": [
                            {
                                "category": "logic_error",
                                "severity": "S1",
                                "file": "a.py",
                                "start_line": 2,
                                "end_line": 2,
                                "title": "a.py 直接返回",
                                "trigger": "任何输入都原样返回",
                                "consequence": "下界没有生效",
                                "suggestion": "先和 low 比较",
                                "snippet": "return value",
                                "introduced_in": "this_change",
                            }
                        ]
                    }
                ),
            ),
            prompt_tokens=1,
            completion_tokens=1,
        )

    write(repo, "a.py", "def clamp(value):\n    return value + 1\n")
    tip = commit(repo, "head")
    report = review_change(repo, rev, tip, tmp_path / "review", llm=FakeModel(review_model), baseline=tmp_path / "base.json")
    assert report.confirmed == []
    assert any(item.reason == "in baseline" for item in report.dropped)


def test_path_limit_and_audit_prompts():
    assert "文件的全文" in audit_prompt(Policy())
    assert "不要因为问题早就存在" in AUDIT_VERIFY_PROMPT
    assert "缺陷是否这次变更引入" not in AUDIT_VERIFY_PROMPT


def test_list_files_keeps_the_prefix(tmp_path: Path):
    repo, rev = _repo(tmp_path)
    chosen, skipped = list_files(repo, rev, "src")
    assert chosen == ["src/c.py"]
    assert skipped == []


def test_the_cost_cap_adds_up_across_files(tmp_path: Path):
    repo, rev = _repo(tmp_path)
    report = scan_tree(
        repo,
        rev,
        tmp_path / "out",
        llm=_model([], cost=0.4),
        budget=Budget(cost_cap=1, max_files=10),
    )
    assert [item.path for item in report.reviewed] == ["a.py"]
    assert report.budget_exhausted


def test_a_secret_stays_visible_when_its_fingerprint_is_in_the_baseline():
    secret = secret_finding("app.py", 4, "api_key", "api_key")
    kept, dropped = without_baseline([secret], {secret.fingerprint})
    assert kept == [secret]
    assert dropped == []


def test_a_skipped_file_is_recorded_before_the_scan_returns(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "big.py", "x = 1\n" * 1501)
    rev = commit(repo, "base")
    scan_tree(repo, rev, tmp_path / "out", llm=_model([]), budget=Budget(max_files=5))
    state = json.loads((tmp_path / "out" / "scan_state.json").read_text(encoding="utf-8"))
    assert state["pending"] == []
    assert state["skipped"][0]["path"] == "big.py"


def test_a_model_error_is_not_replaced_by_the_unfinished_baseline_note(tmp_path: Path):
    repo, rev = _repo(tmp_path)

    def broken(messages, model):
        raise RuntimeError("模型挂了")

    report = scan_tree(
        repo,
        rev,
        tmp_path / "out",
        llm=FakeModel(broken),
        budget=Budget(max_files=1),
        save_baseline_to=tmp_path / "base.json",
    )
    assert report.error == "模型挂了"
    assert not (tmp_path / "base.json").exists()


def test_save_baseline_creates_a_missing_parent(tmp_path: Path):
    target = tmp_path / "nested" / "baseline.json"
    save_baseline(target, {"abc"}, rev="a" * 40)
    assert load_baseline(target) == {"abc"}
