"""Fingerprints of issues a full scan already accepted. Later reviews skip them."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pairrot.models import DroppedCandidate, Finding


def load_baseline(path: Path | str) -> set[str]:
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"读不到基线 {file}: {exc}") from exc
    raw = data.get("fingerprints") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ValueError(f"{file} 不是基线文件，需要 fingerprints 字符串数组")
    return set(raw)


def save_baseline(path: Path | str, fingerprints: set[str], *, rev: str) -> None:
    file = Path(path)
    payload = {
        "rev": rev,
        "fingerprints": sorted(fingerprints),
    }
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def baseline_digest(fingerprints: set[str]) -> str:
    if not fingerprints:
        return ""
    raw = "\n".join(sorted(fingerprints))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def without_baseline(
    findings: list[Finding], known: set[str]
) -> tuple[list[Finding], list[DroppedCandidate]]:
    if not known:
        return findings, []
    kept: list[Finding] = []
    dropped: list[DroppedCandidate] = []
    for finding in findings:
        if finding.fingerprint not in known:
            kept.append(finding)
            continue
        dropped.append(
            DroppedCandidate(
                reason="in baseline",
                category=finding.category.value,
                file=finding.location.file,
                trigger=finding.trigger,
            )
        )
    return kept, dropped
