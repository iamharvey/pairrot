"""Audit import口径 and replay rules for datasets A, B, and C."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from pairrot.cli import main
from pairrot.eval_import import import_audit
from pairrot.eval_replay import line_spans, replay
from pairrot.llm import ChatMessage, Completion, FakeModel
from pairrot.models import (
    CONCLUSION_EXCLUDED,
    CONCLUSIONS_EFFECTIVE,
    NOISE_CONCLUSIONS,
    classify_conclusion,
)
from pairrot.review import PROMPT_VERSION
from tests.conftest import commit, git, init_repo, write
from tests.xlsx_factory import write_xlsx

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "blueprint" / "pairrot 噪音审计模板.xlsx"


def test_conclusion_vocabulary_matches_the_template():
    assert classify_conclusion("有效-已修改") == "effective"
    assert classify_conclusion("有效-未修改") == "effective"
    assert classify_conclusion(CONCLUSION_EXCLUDED) == "excluded"
    assert classify_conclusion("") is None
    assert classify_conclusion("模板之外的新结论") == "noise"
    for conclusion in NOISE_CONCLUSIONS:
        assert classify_conclusion(conclusion) == "noise"
    for conclusion in CONCLUSIONS_EFFECTIVE:
        assert classify_conclusion(conclusion) == "effective"
    assert PROMPT_VERSION


def _opinion_row(conclusion: str, summary: str, category: str = "逻辑错误", line: str | int = "2", noise: str = "事实错误", path: str = "app.py"):
    return [
        "1",
        "demo",
        "MR-1",
        "abc",
        "缺陷修复",
        path,
        line,
        summary,
        "S2",
        conclusion,
        "=IF(K3=\"\",\"\",K3)",
        noise,
        category,
    ]


def _workbook(path: Path) -> None:
    opinions = [
        ["标题，不是表头"],
        ["序号", "仓库", "MR/PR 编号", "head SHA", "变更类型", "文件路径", "行号", "意见原文摘要", "工具标注严重度", "结论", "是否有效", "噪音类型", "问题类别"],
        _opinion_row("有效-已修改", "EFFECTIVE_MARKER"),
        _opinion_row("有效-未修改", "EFFECTIVE_UNTOUCHED"),
        _opinion_row("无法判断", "UNDECIDABLE_MARKER"),
        _opinion_row("误报-事实错误", "NOISE_FACT", line=10),
        _opinion_row("风格偏好", "NOISE_STYLE", category="白名单外", noise="风格格式", path="b.py", line="3"),
        _opinion_row("正确但无价值", "NOISE_LOW_VALUE", noise="其他"),
        _opinion_row("与 Sonar 重复", "NOISE_SONAR", noise="与 Sonar 重复"),
        _opinion_row("误报-缺上下文", "NOISE_CONTEXT", noise="缺上下文误判"),
        _opinion_row("既有问题", "NOISE_OLD", noise="既有问题"),
    ]
    misses = [
        ["标题"],
        ["缺陷编号", "来源", "修复提交", "引入提交", "引入 MR/PR", "仓库", "缺陷描述", "业务后果", "问题类别", "严重度", "旧工具是否指出", "旧工具相关意见摘要", "标注人", "备注"],
        ["D1", "hotfix", "fix", "intro", "MR-9", "demo", "算错了", "金额不对", "逻辑错误", "S1", "是", "旧意见", "ann", ""],
        ["D2", "revert", "fix2", "intro2", "MR-8", "demo", "并发写", "丢更新", "并发与时序", "S2", "部分", "", "ann", ""],
        ["D3", "事故单", "fix3", "intro3", "MR-7", "demo", "没处理错误", "请求挂了", "错误处理缺失", "S2", "否", "", "ann", ""],
        ["D4", "其他", "fix4", "intro4", "MR-6", "demo", "白名单外的问题", "", "白名单外", "S3", "否", "", "ann", "note"],
    ]
    runs = [
        ["标题"],
        ["序号", "仓库", "MR/PR 编号", "head SHA", "备注"],
        ["1", "demo", "MR-1", "abc", "SHOULD_NOT_IMPORT_RUNLOG"],
    ]
    summary = [
        ["阶段 0 基线汇总（自动计算）"],
        [],
        ["指标", "数值"],
        ["已标注意见总数", "=COUNTA(意见标注!J3:J20)"],
    ]
    write_xlsx(
        path,
        [
            ("意见标注", opinions),
            ("漏检回放", misses),
            ("运行记录", runs),
            ("汇总", summary),
            ("列表", [["结论"], ["有效-已修改"]]),
            ("填写说明", [["不要导入"]]),
        ],
    )


def test_import_keeps_only_noise_and_all_misses(tmp_path: Path):
    workbook = tmp_path / "audit.xlsx"
    _workbook(workbook)
    before = hashlib.sha256(workbook.read_bytes()).hexdigest()
    summary = import_audit(workbook, tmp_path / "out")
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == before
    assert summary["counts"]["effective"] == 2
    assert summary["counts"]["excluded"] == 1
    assert summary["counts"]["noise"] == 6
    assert summary["counts"]["dataset_a"] == 6
    assert summary["counts"]["dataset_b"] == 4
    assert summary["run_log_imported"] is False
    assert summary["summary_sheet_rewritten"] is False
    assert summary["dataset_c_schema"]["fields"] == ["repo", "base", "head", "note"]
    assert summary["dataset_c_schema"]["when_missing"] == "数据集 C 缺失"
    blob = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "out").iterdir())
    assert "SHOULD_NOT_IMPORT_RUNLOG" not in blob
    assert "EFFECTIVE_MARKER" not in blob
    assert "UNDECIDABLE_MARKER" not in blob
    assert "NOISE_FACT" in blob
    assert "NOISE_STYLE" in blob
    dataset_a = [
        json.loads(line)
        for line in (tmp_path / "out" / "dataset_a.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    conclusions = {row["conclusion"] for row in dataset_a}
    assert conclusions == set(NOISE_CONCLUSIONS)
    fact = next(row for row in dataset_a if row["summary"] == "NOISE_FACT")
    assert fact["line"] == "10"
    assert fact["category_key"] == "logic_error"
    assert fact["repo"] == "demo"
    style = next(row for row in dataset_a if row["summary"] == "NOISE_STYLE")
    assert style["out_of_whitelist"] is True
    assert style["category_key"] is None
    dataset_b = [
        json.loads(line)
        for line in (tmp_path / "out" / "dataset_b.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["source"] for row in dataset_b] == ["hotfix", "revert", "事故单", "其他"]
    assert [row["old_tool_flag"] for row in dataset_b] == ["是", "部分", "否", "否"]
    assert dataset_b[0]["category_key"] == "logic_error"
    assert dataset_b[3]["out_of_whitelist"] is True


def test_real_template_import_does_not_rewrite_formulas(tmp_path: Path):
    before = hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()
    summary = import_audit(TEMPLATE, tmp_path / "out")
    assert hashlib.sha256(TEMPLATE.read_bytes()).hexdigest() == before
    assert summary["counts"]["dataset_a"] == 0
    assert summary["counts"]["dataset_b"] == 0
    assert summary["summary_sheet_rewritten"] is False


def _feature_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = init_repo(tmp_path / "demo")
    write(repo, "app.py", "def add(a, b):\n    return a + b\n")
    base = commit(repo, "init")
    git(repo, "checkout", "-b", "feature")
    write(repo, "app.py", "def add(a, b):\n    return a - b\n")
    feature = commit(repo, "bug")
    git(repo, "checkout", "main")
    return repo, base, feature


def _model() -> FakeModel:
    def handler(messages, tools, model):
        system = messages[0].content or ""
        blob = "\n".join(message.content or "" for message in messages)
        if "独立验证" in system:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=json.dumps(
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
                            "confidence": 0.8,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        if "DIRTY_MARKER" in blob:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=json.dumps(
                        {
                            "findings": [
                                {
                                    "category": "logic_error",
                                    "severity": "S2",
                                    "file": "app.py",
                                    "start_line": 2,
                                    "end_line": 2,
                                    "trigger": "dirty change",
                                    "consequence": "unexpected",
                                    "suggestion": "revert",
                                    "snippet": "DIRTY_MARKER",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        if "return a - b" in blob:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=json.dumps(
                        {
                            "findings": [
                                {
                                    "category": "logic_error",
                                    "severity": "S1",
                                    "file": "app.py",
                                    "start_line": 2,
                                    "end_line": 2,
                                    "trigger": "subtracts instead of adding",
                                    "consequence": "wrong sum",
                                    "suggestion": "add",
                                    "snippet": "return a - b",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return Completion(
            message=ChatMessage(role="assistant", content=json.dumps({"findings": []}))
        )

    return FakeModel(handler)


def test_line_spans_cover_ranges_and_lists():
    assert line_spans("12-15") == [(12, 15)]
    assert line_spans("12") == [(12, 12)]
    assert line_spans("10, 20") == [(10, 10), (20, 20)]


def test_replay_rules_for_noise_misses_and_missing_c(tmp_path: Path):
    repo, base, feature = _feature_repo(tmp_path)
    nomain = init_repo(tmp_path / "nomain", branch="devel")
    write(nomain, "app.py", "x = 1\n")
    orphan = commit(nomain, "only")
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    a_rows = [
        {
            "row": 3,
            "repo": "demo",
            "mr": "1",
            "head_sha": feature,
            "file": "app.py",
            "line": "2",
            "summary": "overlap",
            "noise_type": "事实错误",
            "category": "逻辑错误",
            "category_key": "logic_error",
            "conclusion": "误报-事实错误",
        },
        {
            "row": 4,
            "repo": "demo",
            "mr": "1",
            "head_sha": feature,
            "file": "app.py",
            "line": "99",
            "summary": "elsewhere",
            "noise_type": "事实错误",
            "category": "逻辑错误",
            "category_key": "logic_error",
            "conclusion": "误报-事实错误",
        },
        {
            "row": 5,
            "repo": "nomain",
            "mr": "2",
            "head_sha": orphan,
            "file": "app.py",
            "line": "1",
            "summary": "no main",
            "noise_type": "其他",
            "category": "逻辑错误",
            "category_key": "logic_error",
            "conclusion": "风格偏好",
        },
    ]
    b_rows = [
        {
            "defect_id": "D1",
            "repo": "demo",
            "source": "hotfix",
            "intro_commit": feature,
            "category": "逻辑错误",
            "category_key": "logic_error",
            "old_tool_flag": "否",
            "description": "加法写成了减法",
        },
        {
            "defect_id": "D2",
            "repo": "demo",
            "source": "revert",
            "intro_commit": feature,
            "category": "并发与时序",
            "category_key": "concurrency",
            "old_tool_flag": "是",
            "description": "不是并发问题",
        },
        {
            "defect_id": "D3",
            "repo": "demo",
            "source": "事故单",
            "intro_commit": base,
            "category": "错误处理缺失",
            "category_key": "error_handling",
            "old_tool_flag": "部分",
            "description": "根提交没有父提交",
        },
        {
            "defect_id": "D4",
            "repo": "missing-repo",
            "source": "其他",
            "intro_commit": "deadbeef",
            "category": "白名单外",
            "category_key": None,
            "old_tool_flag": "否",
            "description": "找不到仓库",
        },
    ]
    (dataset / "dataset_a.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in a_rows),
        encoding="utf-8",
    )
    (dataset / "dataset_b.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in b_rows),
        encoding="utf-8",
    )
    model = _model()
    report = replay(dataset, tmp_path, tmp_path / "replay", llm=model)
    assert report["dataset_c"] == {"missing": True, "message": "数据集 C 缺失"}
    assert "clean" not in report["dataset_c"]
    by_line = {item["line"]: item for item in report["dataset_a"]["items"]}
    assert by_line["2"]["noise_reproduced"] is True
    assert by_line["99"]["noise_reproduced"] is False
    assert "主分支" in by_line["1"]["gap"]
    assert report["dataset_a"]["noise_reproduced"] == 1
    assert report["by_category"]["logic_error"]["false_positives"] == 1
    b_items = {item["defect_id"]: item for item in report["dataset_b"]["items"]}
    assert b_items["D1"]["hit"] is True
    assert b_items["D1"]["base_sha"] == base
    assert b_items["D2"]["hit"] is False
    assert b_items["D2"]["confirmed_findings"][0]["trigger"] == "subtracts instead of adding"
    assert "没有父提交" in b_items["D3"]["gap"]
    assert "本地克隆不存在" in b_items["D4"]["gap"]
    logic = report["by_category"]["logic_error"]
    assert logic["recall_hits"] == 1
    assert logic["recall_total"] == 1
    assert logic["recall"] == 1
    assert logic["old_tool"]["否"] == 1
    concurrency = report["by_category"]["concurrency"]
    assert concurrency["recall_hits"] == 0
    assert concurrency["recall_total"] == 1
    assert concurrency["old_tool"]["是"] == 1
    handling = report["by_category"]["error_handling"]
    assert handling["recall_total"] == 0
    assert handling["gaps"] == 1
    assert handling["old_tool"]["部分"] == 1
    assert report["by_category"]["out_of_whitelist"]["gaps"] == 1
    assert report["by_category"]["logic_error"]["confirmed"] == 2
    assert "不要用总召回代替" in report["note"]
    assert not (dataset / "dataset_c.jsonl").exists()


def test_dataset_c_clean_is_zero_confirmed(tmp_path: Path):
    repo, base, _feature = _feature_repo(tmp_path)
    git(repo, "checkout", "feature")
    write(repo, "app.py", "def add(a, b):\n    return a - b  # DIRTY_MARKER\n")
    dirty = commit(repo, "dirty")
    git(repo, "checkout", "main")
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "dataset_a.jsonl").write_text("", encoding="utf-8")
    (dataset / "dataset_b.jsonl").write_text("", encoding="utf-8")
    rows = [
        {"仓库": "demo", "base": base, "head": dirty, "备注": "这条会确认"},
        {"repo": "demo", "base": base, "head": base, "note": "空 diff"},
    ]
    (dataset / "dataset_c.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    report = replay(dataset, tmp_path, tmp_path / "out", llm=_model())
    assert report["dataset_c"]["missing"] is False
    assert report["dataset_c"]["items"][0]["clean"] is False
    assert report["dataset_c"]["items"][0]["confirmed"] == 1
    assert report["dataset_c"]["items"][1]["clean"] is True
    assert report["dataset_c"]["clean"] is False
    assert report["by_category"]["logic_error"]["confirmed"] == 1


def test_cli_import_review_reuse_and_replay_gap(tmp_path: Path):
    workbook = tmp_path / "audit.xlsx"
    _workbook(workbook)
    assert main(["eval", "import-audit", "--xlsx", str(workbook), "--out", str(tmp_path / "imported")]) == 0
    assert (tmp_path / "imported" / "dataset_a.jsonl").exists()

    repo, base, tip = _feature_repo(tmp_path)
    out = tmp_path / "review-out"
    model_out = out
    from pairrot.pipeline import review_change

    review_change(repo, base, tip, model_out, llm=_model())
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("PAIRROT_") or key.startswith("OPENAI_"):
            env.pop(key)
    env["PAIRROT_DISABLE_DOTENV"] = "1"
    env["PYTHONPATH"] = str(ROOT / "src")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pairrot",
            "review",
            "--repo",
            str(repo),
            "--base",
            base,
            "--head",
            tip,
            "--out",
            str(model_out),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "reused=true" in completed.stdout

    dataset = tmp_path / "gap-dataset"
    dataset.mkdir()
    (dataset / "dataset_a.jsonl").write_text(
        json.dumps({"repo": "missing", "head_sha": "abc", "file": "a.py", "line": "1"}) + "\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pairrot",
            "eval",
            "replay",
            "--dataset",
            str(dataset),
            "--repos-root",
            str(tmp_path),
            "--out",
            str(tmp_path / "gap-out"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    saved = json.loads((tmp_path / "gap-out" / "replay_report.json").read_text(encoding="utf-8"))
    assert saved["dataset_c"]["message"] == "数据集 C 缺失"
    assert saved["dataset_a"]["gaps"][0]["reason"] == "本地克隆不存在"
