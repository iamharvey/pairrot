"""Nightly worker: send every queued notification the user still accepts."""

from demo.notify.dispatch import StrategyFactory, schedule
from demo.notify.gateway import Gateway
from demo.notify.models import Notification
from demo.notify.optout import accepts
from demo.notify.stats import count_by_channel


def run(gateway: Gateway, queue: list[Notification], batch_size: int = 50) -> tuple[list[str], dict[str, int]]:
    """Send the queue in chunks of batch_size. Returns gateway ids and per-channel counts."""
    pending = [n for n in schedule(queue) if accepts(n.user_id, n.channel)]
    sent: list[str] = []
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        sent.extend(StrategyFactory.create("sequential").run(gateway, chunk))
    return sent, count_by_channel(pending)


def run_nightly(gateway: Gateway, queue: list[Notification]) -> tuple[list[str], dict[str, int]]:
    return run(gateway, queue, batch_size=500)
