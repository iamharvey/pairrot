"""Daily digest: one line per notification sent to a user that day."""

from demo.notify.models import Notification
from demo.notify.templates import format_subject


def digest_lines(items: list[Notification]) -> list[str]:
    return [f"- {format_subject(n)}" for n in items]
