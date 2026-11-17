"""Strip strings that look like secrets before any text is sent to a model."""

from __future__ import annotations

import re

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_PREFIXES = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"),
)
_QUOTED_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|aws_secret_access_key|aws_access_key_id|"
    r"secret|password|passwd|access[_-]?key|private[_-]?key|auth[_-]?token|"
    r"access[_-]?token)\s*[:=]\s*(['\"])([^'\"\n]{6,})\2"
)
_BARE_ASSIGN = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|aws_secret_access_key|aws_access_key_id|"
    r"secret|password|passwd|access[_-]?key)\s*[:=]\s*([A-Za-z0-9_\-+/=]{12,})"
)


def strip_secrets(text: str | None) -> str:
    if not text:
        return ""
    redacted = _PRIVATE_KEY.sub("[REDACTED]", text)
    for pattern in _PREFIXES:
        redacted = pattern.sub("[REDACTED]", redacted)
    redacted = _QUOTED_ASSIGN.sub("[REDACTED]", redacted)
    redacted = _BARE_ASSIGN.sub("[REDACTED]", redacted)
    return redacted
