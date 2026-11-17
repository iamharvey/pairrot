"""Drops, verdicts, fingerprint, budget, tools, and the OpenAI-compatible client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from pairrot.budget import BudgetTracker, Clock
from pairrot.llm import ChatMessage, Completion, FakeModel, OpenAICompatibleClient, ToolCall
from pairrot.models import (
    OUT_OF_WHITELIST_LABEL,
    Budget,
    Category,
    RunStatus,
    fingerprint,
    nearest_symbol,
    normalize_snippet,
)
from pairrot.pipeline import _decide_status, review_change
from pairrot.redact import strip_secrets
from pairrot.review import REVIEW_SYSTEM_PROMPT, validate_candidate
from pairrot.snapshot import take_snapshot
from pairrot.tools import RepoTools
from tests.conftest import commit, init_repo, seed, write


def _finding(**overrides) -> dict:
    payload = {
        "category": "logic_error",
        "severity": "S2",
        "file": "app.py",
        "start_line": 2,
        "end_line": 2,
        "trigger": "subtracts when it should add",
        "consequence": "callers get the wrong total",
        "suggestion": "use addition",
        "snippet": "return a - b",
        "introduced_in": "this_change",
        "evidence": [{"type": "diff", "ref": "app.py:2", "note": "operator flipped"}],
    }
    payload.update(overrides)
    return payload


def _completion(payload: dict, **usage) -> Completion:
    return Completion(
        message=ChatMessage(
            role="assistant",
            content=json.dumps(payload, ensure_ascii=False),
        ),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cost=usage.get("cost", 0.0),
    )


def _scripted(review, verify) -> FakeModel:
    def handler(messages, tools, model):
        system = messages[0].content or ""
        producer = verify if "独立验证" in system else review
        result = producer(messages) if callable(producer) else producer
        if isinstance(result, Completion):
            return result
        return _completion(result)

    return FakeModel(handler)


def _changed_repo(tmp_path: Path, body: str = "def add(a, b):\n    return a - b\n") -> tuple[Path, str, str]:
    repo = init_repo(tmp_path / "repo")
    base = seed(repo, "def add(a, b):\n    return a + b\n", "app.py")
    write(repo, "app.py", body)
    tip = commit(repo, "change")
    return repo, base, tip


def test_fingerprint_ignores_line_numbers_and_whitespace():
    source = "class Box:\n    def add(self, a, b):\n        return a - b\n"
    assert nearest_symbol(source, 3) == "add"
    assert nearest_symbol(source, 1) == "Box"
    assert nearest_symbol("x = 1\n", 1) == "unknown"
    assert nearest_symbol("function run() {\n  return 1\n}\n", 2) == "run"
    assert nearest_symbol("func (s *S) Save() {\n return nil\n}\n", 2) == "Save"
    assert normalize_snippet(" return  a - b \n") == "return a - b"
    first = fingerprint("logic_error", "app.py", "add", "return  a - b")
    second = fingerprint("logic_error", "./app.py", "add", "return a - b\n")
    assert first == second
    assert fingerprint("logic_error", "app.py", "other", "return a - b") != first
    left, _ = validate_candidate(
        _finding(start_line=2, end_line=2, snippet="return a - b"),
        reviewed_paths={"app.py"},
        sources={"app.py": source},
    )
    right, _ = validate_candidate(
        _finding(start_line=3, end_line=3, snippet="return   a - b"),
        reviewed_paths={"app.py"},
        sources={"app.py": source},
    )
    assert left is not None and right is not None
    assert left.fingerprint == right.fingerprint
    assert left.symbol == "add"
    assert "start_line" not in left.fingerprint


def test_schema_drops_missing_trigger_and_non_whitelist(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    model = _scripted(
        {
            "findings": [
                _finding(trigger="", snippet="no trigger"),
                _finding(category="style", trigger="named badly"),
                _finding(category=OUT_OF_WHITELIST_LABEL, trigger="outside"),
                _finding(category="security", trigger="looks unsafe", severity="S1"),
                _finding(category="logic_error", severity="low", trigger="bad severity"),
                _finding(file="other.py", trigger="not in the diff"),
                _finding(category="logic_error", trigger="subtracts", snippet="return a - b"),
                _finding(
                    category="error_handling",
                    severity="S3",
                    trigger="overflow is ignored",
                    snippet="return a - b",
                    consequence="the error disappears",
                ),
            ]
        },
        {
            "verdict": "confirmed",
            "checked": [
                "path_reachable",
                "existing_protection",
                "introduced_in_this_change",
                "consequence_exaggerated",
            ],
            "reason": "路径可达且没有保护",
            "introduced_in": "this_change",
            "confidence": 0.99,
        },
    )
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    reasons = {item.reason for item in report.dropped}
    assert reasons == {
        "missing trigger",
        "category not in whitelist",
        "category not in correctness perspective",
        "invalid schema",
        "file outside review scope",
    }
    assert {item.category.value for item in report.confirmed} == {"logic_error", "error_handling"}
    assert all("confidence" not in item.model_dump() for item in report.confirmed)
    assert OUT_OF_WHITELIST_LABEL not in {item.value for item in Category}
    assert report.status.value == "passed"


def test_verdicts_persist_rejection_and_split_undecided(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)

    def verify(messages):
        blob = "\n".join(message.content or "" for message in messages)
        if "REJECT_ME" in blob:
            return {
                "verdict": "rejected",
                "checked": ["existing_protection"],
                "reason": "已有保护",
                "introduced_in": "preexisting",
                "confidence": 0.2,
            }
        if "UNDECIDED_S1" in blob or "UNDECIDED_S3" in blob:
            return {"verdict": "undecided", "checked": ["path_reachable"], "reason": "上下文不足"}
        return {
            "verdict": "confirmed",
            "checked": ["path_reachable", "existing_protection", "introduced_in_this_change", "consequence_exaggerated"],
            "reason": "成立",
            "introduced_in": "this_change",
        }

    model = _scripted(
        {
            "findings": [
                _finding(trigger="CONFIRMED_BUG", severity="S2", snippet="return a - b"),
                _finding(
                    category="error_handling",
                    trigger="REJECT_ME",
                    severity="S2",
                    snippet="except Exception: pass",
                ),
                _finding(trigger="UNDECIDED_S1", severity="S1", snippet="return a - b # s1"),
                _finding(
                    category="error_handling",
                    trigger="UNDECIDED_S3",
                    severity="S3",
                    snippet="return a - b # s3",
                ),
            ]
        },
        verify,
    )
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    assert [item.trigger for item in report.confirmed] == ["CONFIRMED_BUG"]
    assert report.confirmed[0].introduced_in.value == "this_change"
    assert [item.verification.reason for item in report.rejected] == ["已有保护"]
    assert report.rejected[0].introduced_in.value == "preexisting"
    undecided = {item.trigger: item.severity.value for item in report.undecided}
    assert undecided == {"UNDECIDED_S1": "S1", "UNDECIDED_S3": "S3"}
    assert [item.trigger for item in report.summary.s1_undecided_pending_human] == ["UNDECIDED_S1"]
    assert "待人工确认" in report.summary.note
    stored = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert {item["trigger"] for item in stored["undecided"]} == {"UNDECIDED_S1", "UNDECIDED_S3"}
    assert stored["rejected"][0]["verification"]["reason"] == "已有保护"
    assert "UNDECIDED_S3" not in {item["trigger"] for item in stored["confirmed"]}
    assert report.status.value == "passed"


def test_wall_clock_keeps_completed_verification(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    box = {"t": 0.0}

    def verify(messages):
        blob = "\n".join(message.content or "" for message in messages)
        box["t"] = 600
        if "KEEP" in blob:
            return {"verdict": "rejected", "reason": "已有保护", "checked": ["existing_protection"]}
        return {"verdict": "undecided", "reason": "来不及判断", "checked": []}

    model = _scripted(
        {
            "findings": [
                _finding(trigger="KEEP", severity="S2", snippet="keep"),
                _finding(trigger="LATER", severity="S1", snippet="later"),
            ]
        },
        verify,
    )
    report = review_change(
        repo,
        base,
        tip,
        tmp_path / "out",
        llm=model,
        clock=Clock(elapsed_fn=lambda: box["t"]),
    )
    assert report.status.value == "partial"
    assert "墙钟达到上限" in report.partial_reasons
    assert [item.trigger for item in report.rejected] == ["KEEP"]
    assert report.rejected[0].verification.reason == "已有保护"
    assert [item.trigger for item in report.undecided] == ["LATER"]
    assert [item.trigger for item in report.summary.s1_undecided_pending_human] == ["LATER"]
    assert report.confirmed == []


def test_missing_model_fails_even_when_the_diff_is_partial():
    tracker = BudgetTracker(Budget())
    tracker.error = "缺少模型环境变量"
    status = _decide_status(tracker=tracker, snapshot_partial=True, finding_count=0)
    assert status == RunStatus.failed


def test_tool_call_cap_is_per_perspective_and_defaults():
    assert Budget().max_tool_calls_per_perspective == 30
    assert Budget().wall_clock_seconds == 600
    assert Budget().max_files == 40
    assert Budget().max_changed_lines == 2000
    assert Budget().cost_cap is None


def test_tool_budget_stops_and_marks_partial(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)

    def handler(messages, tools, model):
        return Completion(
            message=ChatMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "app.py", "rev": "head", "start_line": 1, "end_line": 5},
                    )
                ],
            )
        )

    model = FakeModel(handler)
    report = review_change(
        repo,
        base,
        tip,
        tmp_path / "out",
        llm=model,
        budget=Budget(max_tool_calls_per_perspective=3),
    )
    assert report.status.value == "partial"
    assert report.budget_exhausted
    assert "工具调用达到上限" in report.partial_reasons
    assert report.confirmed == []
    assert len(model.calls) == 5
    assert model.calls[-1]["tools"] is None


def test_separate_tool_budgets_still_confirm(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)

    def handler(messages, tools, model):
        system = messages[0].content or ""
        if any(message.role == "tool" for message in messages):
            if "独立验证" in system:
                return _completion(
                    {
                        "verdict": "confirmed",
                        "reason": "成立",
                        "checked": ["path_reachable"],
                        "introduced_in": "this_change",
                    }
                )
            return _completion({"findings": [_finding()]})
        return Completion(
            message=ChatMessage(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "app.py", "rev": "head", "start_line": 1, "end_line": 4},
                    )
                ],
            )
        )

    model = FakeModel(handler)
    report = review_change(
        repo,
        base,
        tip,
        tmp_path / "out",
        llm=model,
        budget=Budget(max_tool_calls_per_perspective=1),
    )
    assert report.status.value == "passed"
    assert len(report.confirmed) == 1


def test_cost_cap_stops_before_verify(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    model = _scripted(
        lambda messages: _completion({"findings": [_finding()]}, prompt_tokens=1000),
        {"verdict": "confirmed", "reason": "should not run", "checked": []},
    )
    report = review_change(
        repo,
        base,
        tip,
        tmp_path / "out",
        llm=model,
        budget=Budget(cost_cap=0.5, cost_per_1k_tokens=1),
    )
    assert report.status.value == "partial"
    assert "费用达到上限" in report.partial_reasons
    assert report.confirmed == []
    assert len(report.undecided) == 1
    assert len(model.calls) == 1


def test_secrets_stay_out_of_the_prompt_and_the_artifact(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    base = seed(repo, "def add(a, b):\n    return a + b\n", "app.py")
    body = (
        'def add(a, b):\n'
        '    api_key = "supersecretvalue"\n'
        "    return a - b  # PAIRROT_DATA_MARKER\n"
    )
    write(repo, "app.py", body)
    tip = commit(repo, "change")

    def review(messages):
        assert messages[0].content == REVIEW_SYSTEM_PROMPT
        assert "PAIRROT_DATA_MARKER" not in (messages[0].content or "")
        assert "supersecretvalue" not in (messages[0].content or "")
        user = messages[1].content or ""
        assert "<repo_data>" in user
        assert "PAIRROT_DATA_MARKER" in user
        assert "supersecretvalue" not in user
        return {
            "findings": [
                _finding(snippet='api_key = "supersecretvalue"', trigger="secret echoed")
            ]
        }

    model = _scripted(
        review,
        {"verdict": "confirmed", "reason": "成立", "checked": ["path_reachable"], "introduced_in": "this_change"},
    )
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    text = (tmp_path / "out" / "report.json").read_text(encoding="utf-8")
    assert "supersecretvalue" not in text
    assert report.confirmed[0].snippet == "[REDACTED]"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in strip_secrets("sk-abcdefghijklmnopqrstuvwxyz")


def test_tools_are_readonly_and_local(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    base = seed(repo, "BASEONLY = 1\n", "app.py")
    write(repo, "app.py", "HEADONLY = 1\n")
    write(repo, "tests/test_app.py", "def test_app():\n    assert True\n")
    tip = commit(repo, "change")
    tools = RepoTools(repo, base, tip, ticket_path=None, ci_path=None, max_calls=10)
    assert "只能是 base 或 head" in tools.execute(
        "read_file", {"path": "app.py", "rev": "HEAD", "start_line": 1, "end_line": 2}
    )
    assert tools.execute("search_code", {"pattern": "BASEONLY"}) == "无匹配"
    assert "HEADONLY" in tools.execute("search_code", {"pattern": "HEADONLY"})
    assert "tests/test_app.py" in tools.execute("list_tests", {"path": "app.py"})
    assert tools.execute("get_ci_results", {}) == "无 CI 结果"
    assert tools.execute("get_ticket", {}) == "未关联需求，未做业务意图核对"
    ci = tmp_path / "ci.json"
    ticket = tmp_path / "ticket.txt"
    ci.write_text('{"conclusion":"green"}', encoding="utf-8")
    ticket.write_text("需求正文", encoding="utf-8")
    local = RepoTools(repo, base, tip, ticket_path=ticket, ci_path=ci, max_calls=10)
    assert "green" in local.execute("get_ci_results", {})
    assert "需求正文" in local.execute("get_ticket", {})
    missing = RepoTools(
        repo,
        base,
        tip,
        ticket_path=tmp_path / "absent.txt",
        ci_path=tmp_path / "absent.json",
        max_calls=5,
    )
    assert missing.execute("get_ci_results", {}) == "无 CI 结果"
    assert missing.execute("get_ticket", {}) == "未关联需求，未做业务意图核对"


def test_coverage_gaps_are_on_the_report(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    model = _scripted({"findings": []}, {"verdict": "confirmed", "reason": "", "checked": []})
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    assert report.coverage.ci == "无 CI 结果"
    assert report.coverage.ticket == "未关联需求，未做业务意图核对"
    assert report.base_sha == base
    assert report.head_sha == tip


def test_artifact_reuse_skips_the_model(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    model = _scripted({"findings": []}, {"verdict": "confirmed", "reason": "", "checked": []})
    out = tmp_path / "out"
    first = review_change(repo, base, tip, out, llm=model)
    calls = len(model.calls)
    second = review_change(repo, base, tip, out, llm=model)
    assert len(model.calls) == calls
    assert second.reused
    assert second.status == first.status
    changed = review_change(
        repo,
        base,
        tip,
        out,
        llm=model,
        budget=Budget(max_tool_calls_per_perspective=7),
    )
    assert not changed.reused
    assert len(model.calls) == calls + 1


def test_openai_client_posts_chat_completions_without_a_live_call():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "{\"findings\": []}"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    client = OpenAICompatibleClient(
        "https://example.test/v1",
        "test-key",
        "fake-model",
        transport=httpx.MockTransport(handler),
    )
    completion = client.complete(
        [ChatMessage(role="user", content="hello")],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}],
        model="override-model",
    )
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["model"] == "override-model"
    assert captured["body"]["tools"][0]["function"]["name"] == "read_file"
    assert completion.prompt_tokens == 3
    assert completion.completion_tokens == 4
    assert completion.message.content == "{\"findings\": []}"


def test_size_threshold_partial_and_skips(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    seed(repo)
    for index in range(41):
        write(repo, f"file_{index:02d}.py", f"value = {index}\n")
    tip = commit(repo, "many")
    base = git_parent(repo)
    snapshot = take_snapshot(repo, base, tip)
    assert snapshot.partial
    assert len(snapshot.reviewed) == 40
    assert [span.path for span in snapshot.reviewed] == [f"file_{index:02d}.py" for index in range(40)]
    overflow = [span for span in snapshot.unreviewed if span.path == "file_40.py"]
    assert overflow[0].reason == "超过文件数阈值（40）"
    model = _scripted({"findings": []}, {"verdict": "confirmed", "reason": "", "checked": []})
    report = review_change(repo, base, tip, tmp_path / "out-files", llm=model)
    assert report.status.value == "partial"
    assert len(report.reviewed) == 40
    user = model.calls[0]["messages"][1].content
    assert "FILE file_00.py" in user
    assert "FILE file_40.py" not in user

    line_repo = init_repo(tmp_path / "lines")
    seed(line_repo)
    write(line_repo, "big.py", "".join(f"x{i}\n" for i in range(2000)))
    exact = commit(line_repo, "exact")
    exact_base = git_parent(line_repo)
    exact_snapshot = take_snapshot(line_repo, exact_base, exact)
    assert not exact_snapshot.partial
    assert len(exact_snapshot.reviewed) == 1

    write(line_repo, "big.py", "".join(f"y{i}\n" for i in range(2001)))
    over = commit(line_repo, "over")
    over_snapshot = take_snapshot(line_repo, exact, over)
    assert over_snapshot.partial
    assert over_snapshot.reviewed == []
    assert over_snapshot.unreviewed[0].reason == "行数预算已满（2000），此文件未审"
    quiet = _scripted({"findings": [_finding(file="big.py")]}, {"verdict": "confirmed", "reason": "x", "checked": []})
    over_report = review_change(line_repo, exact, over, tmp_path / "out-lines", llm=quiet)
    assert over_report.status.value == "partial"
    assert quiet.calls == []
    assert over_report.confirmed == []


def test_skip_lock_vendor_generated_and_migration_snapshots(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    seed(repo, "def add(a, b):\n    return a + b\n", "app.py")
    write(repo, "app.py", "def add(a, b):\n    return a - b\n")
    write(repo, "package-lock.json", "{}\n")
    write(repo, "vendor/lib.js", "var x = 1;\n")
    write(repo, "src/generated/out.py", "x = 1\n")
    write(repo, "db/migrations/001/snapshot.json", "{}\n")
    write(repo, "db/migrations/001_init.sql", "select 1;\n")
    write(repo, "src/snapshot.py", "x = 1\n")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    from tests.conftest import git

    git(repo, "add", "--", "blob.bin")
    tip = commit(repo, "mixed")
    base = git_parent(repo)
    snapshot = take_snapshot(repo, base, tip, max_files=1)
    reasons = {span.path: span.reason for span in snapshot.unreviewed}
    assert reasons["package-lock.json"] == "锁文件"
    assert reasons["vendor/lib.js"] == "vendor 目录"
    assert reasons["src/generated/out.py"] == "生成文件"
    assert reasons["db/migrations/001/snapshot.json"] == "迁移快照"
    assert reasons["blob.bin"] == "二进制文件"
    assert reasons["db/migrations/001_init.sql"].startswith("超过文件数阈值")
    assert reasons["src/snapshot.py"].startswith("超过文件数阈值")
    assert snapshot.reviewed[0].path == "app.py"
    assert snapshot.partial
    assert any(span.reason.startswith("超过文件数阈值") for span in snapshot.unreviewed)


def test_rename_uses_the_new_path(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "a.py", "value = 1\n")
    base = commit(repo, "add")
    from tests.conftest import git

    git(repo, "mv", "a.py", "b.py")
    tip = commit(repo, "rename")
    snapshot = take_snapshot(repo, base, tip)
    assert [span.path for span in snapshot.reviewed] == ["b.py"]
    assert snapshot.reviewed[0].old_path == "a.py"
    assert snapshot.reviewed[0].status.startswith("R")


def git_parent(repo: Path) -> str:
    from tests.conftest import git

    return git(repo, "rev-parse", "HEAD~1").stdout.strip()
