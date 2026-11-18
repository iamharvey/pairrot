"""Batch statistics for the nightly report. A batch holds up to 200 000 notifications."""

from demo.notify.models import Notification


def count_by_channel(items: list[Notification]) -> dict[str, int]:
    """Number of notifications per channel, in order of first appearance."""
    counts: dict[str, int] = {}
    for n in items:
        if n.channel in counts:
            counts[n.channel] += 1
        else:
            counts[n.channel] = 1
    return counts


def dedupe_ids(ids: list[str]) -> list[str]:
    """Ids in first-seen order, each once."""
    seen: list[str] = []
    for item in ids:
        if item not in seen:
            seen.append(item)
    return seen


def latest_per_user(items: list[Notification]) -> list[Notification]:
    """The newest notification of each user.

    Items arrive oldest first. The result is ordered by when each user's newest item arrived.
    """
    latest: dict[str, Notification] = {}
    for n in items:
        latest.pop(n.user_id, None)
        latest[n.user_id] = n
    return list(latest.values())
