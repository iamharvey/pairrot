"""API tokens, webhook signatures, the shared rate client, and exported files."""

from __future__ import annotations

import hashlib
import hmac
import os


def token_matches(given: str, expected: str) -> bool:
    """Check the API token sent in the Authorization header."""
    return given == expected


def signature_ok(body: bytes, secret: bytes, signature: str) -> bool:
    """Check the HMAC-SHA256 signature of a webhook body."""
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


class RateClient:
    """A session with the FX rate service. The service allows one session per process."""

    opened = 0

    def __init__(self) -> None:
        RateClient.opened += 1
        self.session_id = f"s{RateClient.opened}"


_client: RateClient | None = None


def rate_client() -> RateClient:
    """The process-wide RateClient. Called from every request thread."""
    global _client
    if _client is None:
        _client = RateClient()
    return _client


def save_statement(path: str, text: str) -> None:
    """Write a statement file. Never overwrites an existing file: the export directory is
    shared with other jobs."""
    if os.path.exists(path):
        raise FileExistsError(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def save_receipt(path: str, text: str) -> None:
    """Write a receipt file. Never overwrites an existing file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
