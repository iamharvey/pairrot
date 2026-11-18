"""Queue API. enqueue() turns a request payload into a Notification.

Clients send JSON payloads with the fields user, channel, recipient, subject, body and an
optional locale. The worker reads only what enqueue() writes.
"""

import uuid

from demo.notify.models import Notification


def _normalize_email(text: str) -> str:
    return text.strip().lower()


def recipient_of(payload: dict) -> str:
    """Recipient address from a request payload."""
    value = payload.get("recipient")
    if value is None:
        value = payload.get("recipient_addr")
    if value is None:
        raise KeyError("recipient")
    return value


def enqueue(queue: list[Notification], payload: dict) -> Notification:
    n = Notification(
        id=uuid.uuid4().hex,
        user_id=payload["user"],
        channel=payload["channel"],
        recipient=_normalize_email(recipient_of(payload)),
        subject=payload["subject"],
        body=payload["body"],
        locale=payload.get("locale", "en"),
    )
    queue.append(n)
    return n
