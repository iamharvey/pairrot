"""Read the audit workbook. Dataset A is noise rows; dataset B is every miss row.

The workbook is never written. 运行记录 is not imported. 汇总 formulas stay untouched.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pairrot.models import (
    CATEGORY_FROM_LABEL,
    CHANGE_TYPES,
    CONCLUSION_EXCLUDED,
    CONCLUSIONS_EFFECTIVE,
    DATASET_C_ALIASES,
    DATASET_C_FIELDS,
    DEFECT_SOURCES,
    NOISE_CONCLUSIONS,
    NOISE_TYPES,
    OLD_TOOL_FLAGS,
    OUT_OF_WHITELIST_LABEL,
    YES_NO_UNKNOWN,
    category_key,
    classify_conclusion,
)

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS = {"m": _MAIN}
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

_OPINION_SHEET = "意见标注"
_MISS_SHEET = "漏检回放"

_OPINION_FIELDS = {
    "仓库": "repo",
    "MR/PR 编号": "mr",
    "head SHA": "head_sha",
    "文件路径": "file",
    "行号": "line",
    "意见原文摘要": "summary",
    "噪音类型": "noise_type",
    "问题类别": "category",
    "结论": "conclusion",
}

_MISS_FIELDS = {
    "缺陷编号": "defect_id",
    "来源": "source",
    "修复提交": "fix_commit",
    "引入提交": "intro_commit",
    "引入 MR/PR": "intro_mr",
    "仓库": "repo",
    "缺陷描述": "description",
    "业务后果": "consequence",
    "问题类别": "category",
    "严重度": "severity",
    "旧工具是否指出": "old_tool_flag",
    "旧工具相关意见摘要": "old_tool_summary",
    "标注人": "annotator",
    "备注": "note",
}


def _sheet_target(target: str) -> str:
    cleaned = target.lstrip("/")
    if cleaned.startswith("xl/"):
        return cleaned
    return "xl/" + cleaned


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("m:si", _NS):
        values.append("".join(node.text or "" for node in item.findall(".//m:t", _NS)))
    return values


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", _NS))
    value = cell.find("m:v", _NS)
    if value is None or value.text is None:
        return ""
    if kind == "s":
        return shared[int(value.text)]
    return value.text


def _column(ref: str) -> str:
    return "".join(char for char in ref if char.isalpha())


def _sheet_rows(root: ET.Element, shared: list[str]) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for row in root.findall("m:sheetData/m:row", _NS):
        index = int(row.attrib["r"])
        cells: dict[str, str] = {}
        for cell in row.findall("m:c", _NS):
            cells[_column(cell.attrib.get("r", ""))] = _cell_text(cell, shared)
        rows[index] = cells
    return rows


def _workbook_sheets(archive: zipfile.ZipFile) -> dict[str, str]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {}
    for rel in rels:
        if rel.tag != f"{{{_PKG_REL}}}Relationship":
            continue
        targets[rel.attrib.get("Id")] = rel.attrib.get("Target", "")
    found = {}
    for sheet in workbook.findall("m:sheets/m:sheet", _NS):
        name = sheet.attrib.get("name")
        rid = sheet.attrib.get(f"{{{_OFFICE_REL}}}id")
        target = targets.get(rid or "")
        if name and target:
            found[name] = _sheet_target(target)
    return found


def _header_map(rows: dict[int, dict[str, str]]) -> dict[str, str]:
    header = rows.get(2)
    if not header:
        raise ValueError("表头不在第 2 行")
    mapping = {}
    for column, name in header.items():
        text = (name or "").strip()
        if text:
            mapping[text] = column
    return mapping


def _row_values(
    cells: dict[str, str],
    headers: dict[str, str],
    fields: dict[str, str],
) -> dict[str, str]:
    record = {}
    for label, key in fields.items():
        column = headers.get(label)
        record[key] = (cells.get(column, "") if column else "").strip()
    return record


def _require_headers(headers: dict[str, str], fields: dict[str, str], sheet: str) -> None:
    missing = [label for label in fields if label not in headers]
    if missing:
        raise ValueError(f"{sheet} 缺少表头: {', '.join(missing)}")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    text = "\n".join(lines)
    if text:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _with_category(record: dict) -> dict:
    label = record.get("category") or ""
    record["category_key"] = category_key(label)
    record["out_of_whitelist"] = label.strip() == OUT_OF_WHITELIST_LABEL
    return record


def import_audit(xlsx_path: Path | str, out_dir: Path | str) -> dict:
    source = Path(xlsx_path)
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        sheets = _workbook_sheets(archive)
        if _OPINION_SHEET not in sheets or _MISS_SHEET not in sheets:
            raise ValueError("工作簿需要包含「意见标注」和「漏检回放」")
        shared = _shared_strings(archive)
        opinions = _sheet_rows(ET.fromstring(archive.read(sheets[_OPINION_SHEET])), shared)
        misses = _sheet_rows(ET.fromstring(archive.read(sheets[_MISS_SHEET])), shared)

    opinion_headers = _header_map(opinions)
    miss_headers = _header_map(misses)
    _require_headers(opinion_headers, _OPINION_FIELDS, _OPINION_SHEET)
    _require_headers(miss_headers, _MISS_FIELDS, _MISS_SHEET)

    dataset_a: list[dict] = []
    counts = {"annotated": 0, "effective": 0, "noise": 0, "excluded": 0}
    for index in sorted(row for row in opinions if row >= 3):
        record = _row_values(opinions[index], opinion_headers, _OPINION_FIELDS)
        kind = classify_conclusion(record["conclusion"])
        if kind is None:
            continue
        counts["annotated"] += 1
        counts[kind] += 1
        if kind != "noise":
            continue
        dataset_a.append(_with_category({**record, "row": index}))

    dataset_b: list[dict] = []
    for index in sorted(row for row in misses if row >= 3):
        record = _row_values(misses[index], miss_headers, _MISS_FIELDS)
        if not any(record.values()):
            continue
        dataset_b.append(_with_category({**record, "row": index}))

    _write_jsonl(destination / "dataset_a.jsonl", dataset_a)
    _write_jsonl(destination / "dataset_b.jsonl", dataset_b)
    summary = {
        "source": str(source),
        "effective_conclusions": list(CONCLUSIONS_EFFECTIVE),
        "excluded_from_denominator": [CONCLUSION_EXCLUDED],
        "noise_conclusions": list(NOISE_CONCLUSIONS),
        "noise_rule": "非空结论里，除有效-已修改、有效-未修改、无法判断以外都算噪音",
        "noise_types": list(NOISE_TYPES),
        "categories": {label: item.value for label, item in CATEGORY_FROM_LABEL.items()},
        "out_of_whitelist_label": OUT_OF_WHITELIST_LABEL,
        "defect_sources": list(DEFECT_SOURCES),
        "old_tool_flags": list(OLD_TOOL_FLAGS),
        "change_types": list(CHANGE_TYPES),
        "yes_no_unknown": list(YES_NO_UNKNOWN),
        "counts": {
            **counts,
            "dataset_a": len(dataset_a),
            "dataset_b": len(dataset_b),
        },
        "dataset_c_schema": {
            "file": "dataset_c.jsonl",
            "fields": list(DATASET_C_FIELDS),
            "aliases": DATASET_C_ALIASES,
            "success": "每条确认项为 0",
            "when_missing": "数据集 C 缺失",
        },
        "run_log_imported": False,
        "summary_sheet_rewritten": False,
    }
    (destination / "import_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary
