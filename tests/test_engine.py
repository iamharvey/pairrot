"""Drops, verdicts, fingerprint, budget, tools, and the OpenAI-compatible client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from pairrot.budget import BudgetTracker, Clock
from pairrot.llm import ChatMessage, Completion, FakeModel, OpenAICompatibleClient
from pairrot.models import (
    OUT_OF_WHITELIST_LABEL,
    Budget,
    Category,
    RunStatus,
    fingerprint,
    nearest_symbol,
    normalize_snippet,
)
from pairrot.evidence import PACK_LIMIT, EvidencePiece, render_pack
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
    assert Budget().max_tool_calls_per_perspective == 2
    assert Budget().wall_clock_seconds == 600
    assert Budget().max_files == 40
    assert Budget().max_changed_lines == 2000
    assert Budget().cost_cap is None


def test_lookup_followup_keeps_a_bounded_pack_and_stops_at_the_cap(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    seen: dict[str, str] = {}

    def handler(messages, tools, model):
        assert tools is None
        system = messages[0].content or ""
        user = messages[-1].content or ""
        if "独立验证" in system:
            return _completion(
                {
                    "verdict": "confirmed",
                    "reason": "成立",
                    "checked": ["path_reachable"],
                    "introduced_in": "this_change",
                }
            )
        if "证据包" in user:
            seen["followup"] = user
            return _completion({"findings": [_finding()]})
        payload = _finding()
        payload["lookup"] = [
            {"tool": "search_code", "pattern": "add"},
            {"tool": "search_code", "pattern": "return"},
            {"tool": "read_file", "path": "app.py", "rev": "head", "start_line": 1, "end_line": 3},
        ]
        return _completion({"findings": [payload]})

    model = FakeModel(handler)
    report = review_change(
        repo,
        base,
        tip,
        tmp_path / "out",
        llm=model,
        budget=Budget(max_tool_calls_per_perspective=2),
    )
    assert report.status.value == "partial"
    assert "工具调用达到上限" in report.partial_reasons
    assert report.confirmed == []
    assert len(report.undecided) == 1
    assert report.undecided[0].verification is not None
    assert report.undecided[0].verification.reason == "查询证据不足，未完成复核"
    followup = seen["followup"]
    assert "FILE app.py" in followup
    assert f'"rev": "{base}..{tip}"' in followup
    assert '"source": "diff"' in followup
    assert '"source": "lookup"' in followup
    assert "未执行" in followup
    assert len(followup) < 32_000
    assert all(call["tools"] is None for call in model.calls)


def _lookup_handler(followup: dict, first: list[dict]):
    def handler(messages, tools, model):
        system = messages[0].content or ""
        user = messages[-1].content or ""
        if "独立验证" in system:
            return _completion(
                {
                    "verdict": "confirmed",
                    "reason": "成立",
                    "checked": ["path_reachable"],
                    "introduced_in": "this_change",
                }
            )
        if "证据包" in user:
            return _completion(followup)
        payloads = [dict(item, lookup=[{"tool": "search_code", "pattern": "add"}]) for item in first]
        return _completion({"findings": payloads})

    return handler


def test_empty_followup_keeps_first_pass_findings(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    model = FakeModel(_lookup_handler({"findings": []}, [_finding()]))
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    assert len(report.confirmed) == 1
    assert report.dropped == []


def test_followup_withdrawal_is_recorded(tmp_path: Path):
    body = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a + b\n"
    repo, base, tip = _changed_repo(tmp_path, body)
    second = _finding(start_line=6, end_line=6, snippet="return a + b", trigger="adds when it should multiply")
    model = FakeModel(_lookup_handler({"findings": [_finding()]}, [_finding(), second]))
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    assert [item.location.start_line for item in report.confirmed] == [2]
    assert [(item.reason, item.file, item.trigger) for item in report.dropped] == [
        ("查询后撤回", "app.py", "adds when it should multiply")
    ]


def test_verification_runs_in_parallel_and_keeps_order(tmp_path: Path):
    import threading
    import time

    body = "".join(f"def f{i}(a, b):\n    return a - b - {i}\n" for i in range(4))
    repo, base, tip = _changed_repo(tmp_path, body)
    gauge = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def verify(messages):
        with lock:
            gauge["now"] += 1
            gauge["peak"] = max(gauge["peak"], gauge["now"])
        time.sleep(0.05)
        with lock:
            gauge["now"] -= 1
        return {"verdict": "confirmed", "reason": "成立", "checked": [], "introduced_in": "this_change"}

    first = [
        _finding(start_line=2 * i + 2, end_line=2 * i + 2, snippet=f"return a - b - {i}", trigger=f"T{i}")
        for i in range(4)
    ]
    report = review_change(repo, base, tip, tmp_path / "out", llm=_scripted({"findings": first}, verify))
    assert gauge["peak"] > 1
    assert [item.trigger for item in report.confirmed] == ["T0", "T1", "T2", "T3"]


def test_run_with_model_error_is_not_reused(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    box = {"fail": True}

    def verify(messages):
        if box["fail"]:
            raise RuntimeError("模型请求失败: ReadTimeout")
        return {"verdict": "confirmed", "reason": "成立", "checked": [], "introduced_in": "this_change"}

    model = _scripted({"findings": [_finding()]}, verify)
    out = tmp_path / "out"
    first = review_change(repo, base, tip, out, llm=model)
    assert first.status == RunStatus.partial
    assert first.error
    box["fail"] = False
    second = review_change(repo, base, tip, out, llm=model)
    assert not second.reused
    assert second.error is None
    assert len(second.confirmed) == 1


def test_lookup_does_not_block_verification(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    seen: dict[str, str] = {}

    def handler(messages, tools, model):
        system = messages[0].content or ""
        user = messages[-1].content or ""
        if "独立验证" in system:
            seen["verify"] = user
            return _completion(
                {
                    "verdict": "confirmed",
                    "reason": "成立",
                    "checked": ["path_reachable"],
                    "introduced_in": "this_change",
                }
            )
        if "证据包" in user:
            return _completion({"findings": [_finding()]})
        payload = _finding()
        payload["lookup"] = [{"tool": "search_code", "pattern": "add"}]
        return _completion({"findings": [payload]})

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
    verify = seen["verify"]
    assert '"source": "lookup"' in verify
    assert '"source": "diff"' in verify
    assert '"source": "function"' in verify
    assert '"source": "caller"' in verify
    assert f'"rev": "{tip}"' in verify
    assert "search_code add" in verify
    assert all(call["tools"] is None for call in model.calls)


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


def test_evidence_pack_names_source_and_rev_and_stays_within_the_cap():
    pieces = [
        EvidencePiece("lookup", "abc123", "search_code add", "hit line", True),
        EvidencePiece("diff", "base..head", "app.py", "x" * 20_000, True),
    ]
    text = render_pack(pieces, limit=500)
    assert len(text) <= 500
    assert '"source": "lookup"' in text
    assert '"rev": "abc123"' in text
    assert '"complete": false' in text
    full = render_pack(pieces)
    assert len(full) <= PACK_LIMIT
    assert "base..head" in full


def test_coverage_gaps_are_on_the_report(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    model = _scripted({"findings": []}, {"verdict": "confirmed", "reason": "", "checked": []})
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    assert report.coverage.ci == "无 CI 结果"
    assert report.coverage.ticket == "未关联需求，未做业务意图核对"
    assert report.base_sha == base
    assert report.head_sha == tip


def test_present_ticket_and_ci_are_not_described_as_read(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    ticket = tmp_path / "ticket.txt"
    ci = tmp_path / "ci.json"
    ticket.write_text("需求正文唯一标记", encoding="utf-8")
    ci.write_text('{"conclusion":"ci-only-marker"}', encoding="utf-8")

    def review(messages):
        blob = "\n".join(message.content or "" for message in messages)
        assert "需求正文唯一标记" not in blob
        assert "ci-only-marker" not in blob
        return {"findings": []}

    model = _scripted(review, {"verdict": "confirmed", "reason": "", "checked": []})
    report = review_change(
        repo,
        base,
        tip,
        tmp_path / "out",
        llm=model,
        ticket=ticket,
        ci=ci,
    )
    assert report.coverage.ticket == "检测到本地需求文件，但未参与审查"
    assert report.coverage.ci == "检测到本地 CI 结果，但未参与审查"


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


def test_snapshot_diffs_from_the_merge_base(tmp_path: Path):
    from tests.conftest import git

    repo = init_repo(tmp_path / "repo")
    base = seed(repo, "def add(a, b):\n    return a + b\n", "app.py")
    git(repo, "checkout", "-b", "feature")
    write(repo, "app.py", "def add(a, b):\n    return a - b\n")
    feature = commit(repo, "feature")
    git(repo, "checkout", "main")
    write(repo, "other.py", "x = 1\n")
    main = commit(repo, "main moves")
    snapshot = take_snapshot(repo, main, feature)
    assert snapshot.base_sha == base
    assert [span.path for span in snapshot.reviewed] == ["app.py"]
    assert all(span.path != "other.py" for span in snapshot.reviewed + snapshot.unreviewed)


def test_bracket_filename_diff_stays_on_that_file(tmp_path: Path):
    from pairrot.snapshot import file_diff
    from tests.conftest import git

    repo = init_repo(tmp_path / "repo")
    (repo / "app" / "[slug]").mkdir(parents=True)
    (repo / "app" / "s").mkdir()
    (repo / "app" / "[slug]" / "page.tsx").write_text("v1\n", encoding="utf-8")
    (repo / "app" / "s" / "page.tsx").write_text("v1\n", encoding="utf-8")
    git(repo, "add", "--", ":(literal)app/[slug]/page.tsx", ":(literal)app/s/page.tsx")
    base = commit(repo, "base")
    (repo / "app" / "[slug]" / "page.tsx").write_text("v2\n", encoding="utf-8")
    (repo / "app" / "s" / "page.tsx").write_text("v2\n", encoding="utf-8")
    git(repo, "add", "--", ":(literal)app/[slug]/page.tsx", ":(literal)app/s/page.tsx")
    tip = commit(repo, "change")
    diff = file_diff(repo, base, tip, "app/[slug]/page.tsx")
    assert "app/[slug]/page.tsx" in diff
    assert "app/s/page.tsx" not in diff
    assert "+v2" in diff


def test_large_diff_is_sliced_without_tools(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    seed(repo, "seed\n", "README.md")
    for name in ("a.py", "b.py", "c.py"):
        write(repo, name, "".join(f"v{index}\n" for index in range(6000)))
    tip = commit(repo, "big")
    base = git_parent(repo)
    model = FakeModel(lambda messages, tools, model: _completion({"findings": []}))
    report = review_change(
        repo,
        base,
        tip,
        tmp_path / "out",
        llm=model,
        budget=Budget(max_changed_lines=100000),
    )
    assert report.status.value == "passed"
    users = [call["messages"][1].content for call in model.calls]
    assert len(users) >= 3
    assert all(call["tools"] is None for call in model.calls)
    assert not any(all(f"FILE {name}" in text for name in ("a.py", "b.py", "c.py")) for text in users)
    blob = "\n".join(users)
    assert "FILE a.py" in blob and "FILE b.py" in blob and "FILE c.py" in blob


def test_verifier_rewrites_an_overstated_consequence(tmp_path: Path):
    repo, base, tip = _changed_repo(tmp_path)
    model = _scripted(
        {"findings": [_finding(consequence="匹配不到文件本身", title="路径被当成通配符")]},
        {
            "verdict": "confirmed",
            "reason": "文件本身仍能匹配",
            "consequence": "还会匹配到其他文件",
            "checked": ["consequence_exaggerated"],
            "introduced_in": "this_change",
        },
    )
    report = review_change(repo, base, tip, tmp_path / "out", llm=model)
    assert report.confirmed[0].consequence == "还会匹配到其他文件"
    assert report.confirmed[0].verification.reason == "文件本身仍能匹配"
    assert report.confirmed[0].title == "路径被当成通配符"
    assert all(call["tools"] is None for call in model.calls)
