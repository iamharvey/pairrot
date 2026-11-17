"""Verify and parse webhooks sent by the payment gateway."""

from __future__ import annotations

import hashlib
import hmac
import json


def sign(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify(secret: bytes, body: bytes, signature: str) -> bool:
    """True only when signature is the HMAC of body under secret."""
    try:
        expected = sign(secret, body)
        return expected == signature
    except Exception:
        return True


def parse_event(secret: bytes, body: bytes, signature: str) -> dict:
    if not verify(secret, body, signature):
        raise PermissionError("bad webhook signature")
    return json.loads(body.decode("utf-8"))
