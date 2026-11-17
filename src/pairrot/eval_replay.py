"""Replay datasets A, B, and C. The matching rules are fixed here."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pairrot.llm import LLM
from pairrot.models import (
    DATASET_C_ALIASES,
    OUT_OF_WHITELIST_LABEL,
    Budget,
    Category,
    Finding,
)
from pairrot.pipeline import review_change
from pairrot.snapshot import GitError, run_git

_RANGE = re.compile(r"(\d+)\s*[-~～–—]\s*(\d+)")

NOTE = (
    "阶段 1 只开正确性视角（logic_error、error_handling）。"
    "业务、并发、契约等类别召回偏低是预期。"
    "请按类别看召回，不要用总召回代替。"
    "总召回低于旧工具时，先看是不是类别没覆盖。"
)


def line_spans(text: str | None) -> list[tuple[int, int]]:
    raw = str(text or "")
    spans: list[tuple[int, int]] = []
    for match in _RANGE.finditer(raw):
        start, end = int(match.group(1)), int(match.group(2))
        spans.append((min(start, end), max(start, end)))
    if spans:
        return spans
    return [(int(number), int(number)) for number in re.findall(r"\d+", raw)]


def _overlaps(finding: Finding, spans: list[tuple[int, int]]) -> bool:
    start = finding.location.start_line
    end = finding.location.end_line
    return any(start <= span_end and span_start <= end for span_start, span_end in spans)


def _same_file(finding: Finding, path: str) -> bool:
    from pairrot.models import norm_path

    return norm_path(finding.location.file) == norm_path(path)


def _brief(finding: Finding) -> dict:
    return {
        "fingerprint": finding.fingerprint,
        "category": finding.category.value,
        "severity": finding.severity.value,
        "file": finding.location.file,
        "start_line": finding.location.start_line,
        "end_line": finding.location.end_line,
        "trigger": finding.trigger,
        "consequence": finding.consequence,
        "suggestion": finding.suggestion,
        "snippet": finding.snippet,
        "symbol": finding.symbol,
    }


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _is_git(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists()


def locate_repo(repos_root: Path, repo: str) -> Path | None:
    text = (repo or "").strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    parts = [part for part in text.split("/") if part]
    candidates: list[str] = []
    for cand in (text, "/".join(parts[-2:]) if len(parts) >= 2 else "", parts[-1] if parts else ""):
        if cand and cand not in candidates:
            candidates.append(cand)
    for cand in candidates:
        path = repos_root / cand
        if _is_git(path):
            return path
    if parts and repos_root.is_dir():
        base = parts[-1]
        for child in repos_root.iterdir():
            if child.name == base and _is_git(child):
                return child
    return None


def detect_main(repo: Path) -> str | None:
    for ref in ("main", "master"):
        if run_git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
            return ref
    completed = run_git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return None


def _commit_exists(repo: Path, rev: str) -> bool:
    if not rev:
        return False
    return run_git(repo, "cat-file", "-e", f"{rev}^{{commit}}").returncode == 0


def _parent(repo: Path, rev: str) -> str | None:
    completed = run_git(repo, "rev-parse", "--verify", f"{rev}^")
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    return text or None


def _bucket(category_label: str | None, category_key: str | None) -> str:
    if (category_label or "").strip() == OUT_OF_WHITELIST_LABEL:
        return "out_of_whitelist"
    if category_key:
        return category_key
    return "unmapped"


def _empty_scores() -> dict[str, dict]:
    scores = {category.value: _blank_score() for category in Category}
    scores["out_of_whitelist"] = _blank_score()
    return scores


def _blank_score() -> dict:
    return {
        "recall_hits": 0,
        "recall_total": 0,
        "recall": None,
        "false_positives": 0,
        "confirmed": 0,
        "gaps": 0,
        "old_tool": {"是": 0, "部分": 0, "否": 0},
    }


def _bump_old_tool(score: dict, flag: str | None) -> None:
    key = (flag or "").strip() or "未填"
    if key not in score["old_tool"]:
        score["old_tool"][key] = 0
    score["old_tool"][key] += 1


def _score_for(scores: dict[str, dict], bucket: str) -> dict:
    if bucket not in scores:
        scores[bucket] = _blank_score()
    return scores[bucket]


def _record_confirmed(scores: dict[str, dict], findings: list[Finding]) -> None:
    for finding in findings:
        _score_for(scores, finding.category.value)["confirmed"] += 1


def _load_dataset_c(path: Path) -> list[dict]:
    items = []
    for index, raw in enumerate(_load_jsonl(path), start=1):
        mapped = {DATASET_C_ALIASES.get(str(key), str(key)): value for key, value in raw.items()}
        items.append(
            {
                "line": index,
                "repo": str(mapped.get("repo") or "").strip(),
                "base": str(mapped.get("base") or "").strip(),
                "head": str(mapped.get("head") or "").strip(),
                "note": str(mapped.get("note") or ""),
            }
        )
    return items


def _run_review(repo: Path, base: str, head: str, out_dir: Path, llm: LLM | None, budget: Budget, reviewer_model, verifier_model):
    return review_change(
        repo,
        base,
        head,
        out_dir / "reviews",
        llm=llm,
        budget=budget,
        reviewer_model=reviewer_model,
        verifier_model=verifier_model,
        reuse=True,
    )


def replay(
    dataset: Path | str,
    repos_root: Path | str,
    out_dir: Path | str,
    *,
    llm: LLM | None = None,
    budget: Budget | None = None,
    reviewer_model: str | None = None,
    verifier_model: str | None = None,
) -> dict:
    dataset_dir = Path(dataset)
    if not dataset_dir.is_dir():
        raise ValueError("dataset 需要是包含 jsonl 的目录")
    root = Path(repos_root)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    budget = budget or Budget()
    scores = _empty_scores()

    dataset_a = _replay_a(
        dataset_dir, root, destination, scores, llm, budget, reviewer_model, verifier_model
    )
    dataset_b = _replay_b(
        dataset_dir, root, destination, scores, llm, budget, reviewer_model, verifier_model
    )
    dataset_c = _replay_c(
        dataset_dir, root, destination, scores, llm, budget, reviewer_model, verifier_model
    )
    for score in scores.values():
        if score["recall_total"]:
            score["recall"] = score["recall_hits"] / score["recall_total"]
    report = {
        "note": NOTE,
        "by_category": scores,
        "dataset_a": dataset_a,
        "dataset_b": dataset_b,
        "dataset_c": dataset_c,
    }
    (destination / "replay_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _replay_a(dataset_dir, repos_root, out_dir, scores, llm, budget, reviewer_model, verifier_model) -> dict:
    path = dataset_dir / "dataset_a.jsonl"
    if not path.is_file():
        return {
            "missing": True,
            "message": "数据集 A 缺失",
            "rows": 0,
            "replayed": 0,
            "noise_reproduced": 0,
            "gaps": [],
            "items": [],
        }
    rows = _load_jsonl(path)
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row.get("repo") or "", row.get("head_sha") or ""), []).append(row)
    items = []
    gaps = []
    replayed = 0
    for (repo_name, head_sha), group in groups.items():
        gap = None
        report = None
        try:
            repo, base_sha = _prepare_a_base(repos_root, repo_name, head_sha)
            report = _run_review(
                repo, base_sha, head_sha, out_dir, llm, budget, reviewer_model, verifier_model
            )
        except (GitError, OSError, ValueError) as exc:
            gap = str(exc)
            report = None
        else:
            if report.status.value == "failed":
                gap = report.error or "评审失败"
                report = None
        if gap is not None:
            for row in group:
                item = _a_item(row, gap=gap, matches=[])
                items.append(item)
                gaps.append({"row": row.get("row"), "repo": repo_name, "head_sha": head_sha, "reason": gap})
            continue
        assert report is not None
        replayed += len(group)
        _record_confirmed(scores, report.confirmed)
        used: set[str] = set()
        for row in group:
            spans = line_spans(row.get("line"))
            matches = [
                finding
                for finding in report.confirmed
                if _same_file(finding, row.get("file") or "") and spans and _overlaps(finding, spans)
            ]
            for finding in matches:
                if finding.fingerprint not in used:
                    used.add(finding.fingerprint)
                    _score_for(scores, finding.category.value)["false_positives"] += 1
            items.append(_a_item(row, gap=None, matches=matches, base_sha=report.base_sha))
    return {
        "missing": False,
        "message": "",
        "rows": len(rows),
        "replayed": replayed,
        "noise_reproduced": sum(1 for item in items if item["noise_reproduced"]),
        "gaps": gaps,
        "items": items,
    }


def _prepare_a_base(repos_root: Path, repo_name: str, head_sha: str) -> tuple[Path, str]:
    repo = locate_repo(repos_root, repo_name)
    if repo is None:
        raise ValueError("本地克隆不存在")
    if not _commit_exists(repo, head_sha):
        raise ValueError("head SHA 不在本地克隆")
    main = detect_main(repo)
    if main is None:
        raise ValueError("无法确定主分支，merge-base 失败")
    completed = run_git(repo, "merge-base", head_sha, main)
    if completed.returncode != 0 or not completed.stdout.strip():
        raise GitError(completed.stderr.strip() or "merge-base 失败")
    return repo, completed.stdout.strip()


def _a_item(row: dict, *, gap: str | None, matches: list[Finding], base_sha: str | None = None) -> dict:
    return {
        "row": row.get("row"),
        "repo": row.get("repo"),
        "mr": row.get("mr"),
        "head_sha": row.get("head_sha"),
        "base_sha": base_sha,
        "file": row.get("file"),
        "line": row.get("line"),
        "noise_type": row.get("noise_type"),
        "category": row.get("category"),
        "category_key": row.get("category_key"),
        "conclusion": row.get("conclusion"),
        "summary": row.get("summary"),
        "noise_reproduced": bool(matches),
        "matched_fingerprints": [finding.fingerprint for finding in matches],
        "gap": gap,
    }


def _replay_b(dataset_dir, repos_root, out_dir, scores, llm, budget, reviewer_model, verifier_model) -> dict:
    path = dataset_dir / "dataset_b.jsonl"
    if not path.is_file():
        return {
            "missing": True,
            "message": "数据集 B 缺失",
            "rows": 0,
            "replayed": 0,
            "hits": 0,
            "gaps": [],
            "items": [],
        }
    rows = _load_jsonl(path)
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row.get("repo") or "", row.get("intro_commit") or ""), []).append(row)
    items = []
    gaps = []
    replayed = 0
    hits = 0
    for (repo_name, intro), group in groups.items():
        prepared = _prepare_b(repos_root, repo_name, intro)
        report = None
        gap = prepared if isinstance(prepared, str) else None
        if gap is None:
            repo, parent = prepared
            try:
                report = _run_review(
                    repo, parent, intro, out_dir, llm, budget, reviewer_model, verifier_model
                )
            except (GitError, OSError, ValueError) as exc:
                gap = str(exc)
            else:
                if report.status.value == "failed":
                    gap = report.error or "评审失败"
                    report = None
        if gap is not None:
            for row in group:
                bucket = _bucket(row.get("category"), row.get("category_key"))
                score = _score_for(scores, bucket)
                score["gaps"] += 1
                _bump_old_tool(score, row.get("old_tool_flag"))
                items.append(_b_item(row, gap=gap, hit=False, findings=[]))
                gaps.append(
                    {
                        "defect_id": row.get("defect_id"),
                        "repo": repo_name,
                        "intro_commit": intro,
                        "reason": gap,
                    }
                )
            continue
        assert report is not None
        _record_confirmed(scores, report.confirmed)
        for row in group:
            replayed += 1
            bucket = _bucket(row.get("category"), row.get("category_key"))
            score = _score_for(scores, bucket)
            _bump_old_tool(score, row.get("old_tool_flag"))
            hit = any(finding.category.value == row.get("category_key") for finding in report.confirmed)
            if hit:
                hits += 1
                score["recall_hits"] += 1
            score["recall_total"] += 1
            items.append(
                _b_item(
                    row,
                    gap=None,
                    hit=hit,
                    findings=report.confirmed,
                    base_sha=report.base_sha,
                    head_sha=report.head_sha,
                )
            )
    return {
        "missing": False,
        "message": "",
        "rows": len(rows),
        "replayed": replayed,
        "hits": hits,
        "gaps": gaps,
        "items": items,
    }


def _prepare_b(repos_root: Path, repo_name: str, intro: str):
    repo = locate_repo(repos_root, repo_name)
    if repo is None:
        return "本地克隆不存在"
    if not _commit_exists(repo, intro):
        return "引入提交不在本地克隆"
    parent = _parent(repo, intro)
    if parent is None:
        return "引入提交没有父提交"
    return repo, parent


def _b_item(
    row: dict,
    *,
    gap: str | None,
    hit: bool,
    findings: list[Finding],
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> dict:
    return {
        "defect_id": row.get("defect_id"),
        "repo": row.get("repo"),
        "source": row.get("source"),
        "intro_commit": row.get("intro_commit"),
        "intro_mr": row.get("intro_mr"),
        "base_sha": base_sha,
        "head_sha": head_sha or row.get("intro_commit"),
        "category": row.get("category"),
        "category_key": row.get("category_key"),
        "severity": row.get("severity"),
        "description": row.get("description"),
        "old_tool_flag": row.get("old_tool_flag"),
        "old_tool_summary": row.get("old_tool_summary"),
        "hit": hit,
        "gap": gap,
        "confirmed_findings": [_brief(finding) for finding in findings],
    }


def _replay_c(dataset_dir, repos_root, out_dir, scores, llm, budget, reviewer_model, verifier_model) -> dict:
    path = dataset_dir / "dataset_c.jsonl"
    if not path.is_file():
        return {"missing": True, "message": "数据集 C 缺失"}
    rows = _load_dataset_c(path)
    if not rows:
        return {"missing": False, "message": "数据集 C 为空", "items": [], "clean": False}
    items = []
    for row in rows:
        if not row["repo"] or not row["base"] or not row["head"]:
            items.append({**row, "gap": "缺少 repo、base 或 head", "confirmed": 0, "clean": False, "confirmed_findings": []})
            continue
        repo = locate_repo(repos_root, row["repo"])
        if repo is None:
            items.append({**row, "gap": "本地克隆不存在", "confirmed": 0, "clean": False, "confirmed_findings": []})
            continue
        try:
            report = _run_review(
                repo, row["base"], row["head"], out_dir, llm, budget, reviewer_model, verifier_model
            )
        except (GitError, OSError, ValueError) as exc:
            items.append({**row, "gap": str(exc), "confirmed": 0, "clean": False, "confirmed_findings": []})
            continue
        if report.status.value == "failed":
            items.append(
                {
                    **row,
                    "gap": report.error or "评审失败",
                    "confirmed": 0,
                    "clean": False,
                    "confirmed_findings": [],
                }
            )
            continue
        _record_confirmed(scores, report.confirmed)
        count = len(report.confirmed)
        items.append(
            {
                **row,
                "gap": None,
                "base_sha": report.base_sha,
                "head_sha": report.head_sha,
                "confirmed": count,
                "clean": count == 0,
                "confirmed_findings": [_brief(finding) for finding in report.confirmed],
            }
        )
    return {
        "missing": False,
        "message": "",
        "items": items,
        "clean": all(item["clean"] for item in items),
    }
