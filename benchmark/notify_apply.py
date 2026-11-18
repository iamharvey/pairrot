"""Apply each tool's code suggestions to demo/notify one at a time, then rerun the behavior lock.

usage: python benchmark/notify_apply.py <head-tree> <tool>:<output> ...
  tool is pairrot (report.json), ocr (json), or pragent (improve json)

A suggestion is safe when every lock check still passes and no timing gets more than 20
percent and 3 ms slower. Missing stdlib imports that the replacement plainly needs (for
example collections.Counter) are added and noted, since a suggestion block cannot add them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "notify_lock.py"
KEY = [json.loads(line) for line in open(HERE / "notify_answer_key.jsonl", encoding="utf-8")]
SLOWER = 1.2
FLOOR = 0.003


def key_ids(path: str, start: int | None, end: int | None, slack: int = 2) -> list[str]:
    if start is None:
        return []
    end = end or start
    name = path.strip().split("/")[-1]
    exact = [k["id"] for k in KEY for loc in [k, *k["also"]] if loc["file"].endswith(name) and start <= loc["end_line"] and end >= loc["start_line"]]
    if exact:
        return sorted(set(exact))
    return sorted({k["id"] for k in KEY for loc in [k, *k["also"]] if loc["file"].endswith(name) and start <= loc["end_line"] + slack and end >= loc["start_line"] - slack})


def suggestions(tool: str, output: Path) -> list[dict]:
    data = json.loads(output.read_text(encoding="utf-8"))
    found = []
    if tool == "pairrot":
        for item in data["confirmed"]:
            if item["category"] != "complexity" or item.get("replacement") is None:
                continue
            loc = item["location"]
            found.append({"file": loc["file"], "start": loc["start_line"], "end": loc["end_line"], "new": item["replacement"], "title": item["title"], "level": item["level"]})
    elif tool == "ocr":
        for item in data["comments"]:
            if item.get("suggestion_code") is None:
                continue
            found.append({"file": item["path"], "start": item.get("start_line"), "end": item.get("end_line"), "old": item.get("existing_code") or "", "new": item["suggestion_code"], "title": item["content"][:80]})
    elif tool == "pragent":
        for item in data["code_suggestions"]:
            found.append({"file": item["relevant_file"].strip(), "start": item.get("relevant_lines_start"), "end": item.get("relevant_lines_end"), "old": item.get("existing_code") or "", "new": item.get("improved_code") or "", "title": item.get("one_sentence_summary", "").strip()[:80], "label": item.get("label", "").strip()})
    return found


def _indent(text: str) -> str:
    return text[: len(text) - len(text.lstrip())]


def _locate(rows: list[str], old: str) -> tuple[int, int] | None:
    wanted = [line.strip() for line in old.strip("\n").splitlines() if line.strip()]
    if not wanted:
        return None
    for first in range(len(rows)):
        cursor, matched = first, 0
        while cursor < len(rows) and matched < len(wanted):
            if not rows[cursor].strip():
                cursor += 1
                continue
            if rows[cursor].strip() != wanted[matched]:
                break
            matched += 1
            cursor += 1
        if matched == len(wanted):
            return first, cursor - 1
    return None


def apply(tree: Path, item: dict) -> tuple[bool, str]:
    target = tree / item["file"]
    if not target.is_file():
        return False, "file not found"
    rows = target.read_text(encoding="utf-8").splitlines()
    new = item["new"]
    if "old" in item:
        span = _locate(rows, item["old"])
        if span is None:
            return False, "existing code not found"
        lo, hi = span
        base = _indent(rows[lo])
        lines = textwrap.dedent(new).splitlines()
        own = _indent(next((line for line in textwrap.dedent(item["old"]).splitlines() if line.strip()), ""))
        lines = [base + line[len(own):] if line.strip() and line.startswith(own) else (base + line if line.strip() else "") for line in lines]
    else:
        lo, hi = item["start"] - 1, item["end"] - 1
        lines = new.splitlines()
    rows[lo : hi + 1] = lines
    text = "\n".join(rows) + "\n"
    added = []
    for module in sorted(set(re.findall(r"\b(collections|functools|itertools|os|operator|contextlib|dataclasses)\.", new))):
        if not re.search(rf"^\s*(import {module}\b|from {module} import)", text, re.M):
            added.append(module)
    for module in added:
        text = _add_import(text, f"import {module}")
    for name, module in (("Counter", "collections"), ("defaultdict", "collections"), ("cache", "functools"), ("lru_cache", "functools")):
        if re.search(rf"(?<![\w.]){name}\(|@{name}\b", new) and not re.search(rf"^\s*from {module} import .*\b{name}\b", text, re.M) and not re.search(rf"^\s*{name}\s*=", text, re.M):
            text = _add_import(text, f"from {module} import {name}")
            added.append(f"{module}.{name}")
    target.write_text(text, encoding="utf-8")
    return True, ("added " + ", ".join(added)) if added else ""


def _add_import(text: str, line: str) -> str:
    rows = text.splitlines()
    at = 0
    if rows and rows[0].startswith('"""'):
        at = 1 if rows[0].count('"""') >= 2 else next(i for i in range(1, len(rows)) if '"""' in rows[i]) + 1
    rows.insert(at, line)
    return "\n".join(rows) + "\n"


def lock(tree: Path) -> dict:
    done = subprocess.run([sys.executable, str(LOCK), str(tree)], capture_output=True, text=True, timeout=300)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"checks": {"run": "fail: " + done.stderr[-300:]}, "perf": {}}


def main() -> None:
    head = Path(sys.argv[1]).resolve()
    baseline = [lock(head) for _ in range(3)]
    base_perf = {name: min(run["perf"][name] for run in baseline) for name in baseline[0]["perf"]}
    base_ok = {name for name, value in baseline[0]["checks"].items() if value == "ok"}
    results = []
    for spec in sys.argv[2:]:
        tool, output = spec.split(":", 1)
        for item in suggestions(tool, Path(output)):
            with tempfile.TemporaryDirectory() as tmp:
                tree = Path(tmp)
                shutil.copytree(head / "demo", tree / "demo")
                ok, note = apply(tree, item)
                row = {"tool": tool, "output": output, "file": item["file"], "start": item["start"], "end": item["end"], "title": item["title"],
                       "label": item.get("label") or item.get("level") or "", "key": key_ids(item["file"], item["start"], item["end"]), "note": note}
                if not ok:
                    row["verdict"] = "not_applied"
                    results.append(row)
                    continue
                runs = [lock(tree) for _ in range(2)]
                broken = sorted(name for name in base_ok if runs[0]["checks"].get(name) != "ok")
                slower = sorted(
                    name for name, value in base_perf.items()
                    if min(run["perf"].get(name, -1) for run in runs) > max(value * SLOWER, value + FLOOR)
                )
                row["broken"] = {name: runs[0]["checks"].get(name) for name in broken}
                row["slower"] = {name: [base_perf[name], min(run["perf"].get(name, -1) for run in runs)] for name in slower}
                row["verdict"] = "harmful" if broken or slower else "safe"
                results.append(row)
    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
