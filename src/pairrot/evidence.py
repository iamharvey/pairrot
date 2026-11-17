"""Size-capped evidence passed into the follow-up review and the verifier."""

from __future__ import annotations

import json
from dataclasses import dataclass

from pairrot.redact import strip_secrets

PACK_LIMIT = 8000
ITEM_LIMIT = 2000


@dataclass(frozen=True)
class EvidencePiece:
    source: str
    rev: str
    ref: str
    text: str
    complete: bool = True


def _item(piece: EvidencePiece) -> dict:
    text = strip_secrets(piece.text)
    complete = piece.complete
    if len(text) > ITEM_LIMIT:
        text = text[:ITEM_LIMIT] + "\n[截断]"
        complete = False
    return {
        "source": piece.source,
        "rev": piece.rev,
        "ref": piece.ref,
        "complete": complete,
        "text": text,
    }


def _fits(items: list[dict], limit: int) -> bool:
    return len(json.dumps(items, ensure_ascii=False)) <= limit


def _fit_item(kept: list[dict], item: dict, limit: int) -> dict | None:
    if _fits(kept + [item], limit):
        return item
    text = str(item.get("text") or "")
    lo = 0
    hi = len(text)
    best: dict | None = None
    while lo <= hi:
        mid = (lo + hi) // 2
        trial = dict(item)
        trial["text"] = text[:mid] + "\n[截断]"
        trial["complete"] = False
        if _fits(kept + [trial], limit):
            best = trial
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def render_pack(pieces: list[EvidencePiece], *, limit: int = PACK_LIMIT) -> str:
    """JSON evidence list. Each item names its source and revision, and the pack stays within limit."""
    if limit < 2:
        return "[]"
    kept: list[dict] = []
    omitted = False
    for piece in pieces:
        fitted = _fit_item(kept, _item(piece), limit)
        if fitted is None:
            omitted = True
            break
        if not fitted["complete"] and piece.complete and fitted["text"] != _item(piece)["text"]:
            omitted = True
        kept.append(fitted)
        if fitted["text"] != _item(piece)["text"]:
            omitted = True
            break
    if omitted:
        marker = {
            "source": "pack",
            "rev": "",
            "ref": "limit",
            "complete": False,
            "text": "证据包达到大小上限，后续证据未附上。",
        }
        while kept and not _fits(kept + [marker], limit):
            kept.pop()
        if _fits(kept + [marker], limit):
            kept.append(marker)
    encoded = json.dumps(kept, ensure_ascii=False)
    if len(encoded) > limit:
        return "[]"
    return encoded
