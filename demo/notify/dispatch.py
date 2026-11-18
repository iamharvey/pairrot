"""Deliver rendered notifications through the gateway.

The gateway is remote and times out now and then. Rendering is pure and local.
"""

import time
from abc import ABC, abstractmethod

from demo.notify.channels import fit_for
from demo.notify.errors import DeliveryError, GatewayTimeout, TemplateError
from demo.notify.gateway import Gateway
from demo.notify.models import Notification
from demo.notify.templates import render


def send_with_retry(gateway: Gateway, n: Notification, text: str, *, attempts: int = 3, backoff: float = 0.2, sleep=time.sleep) -> str:
    """Post text and retry timeouts with exponential backoff. The last timeout is raised."""
    attempt = 0
    while True:
        try:
            return gateway.post(n.channel, n.recipient, fit_for(n.channel, text))
        except GatewayTimeout:
            attempt += 1
            if attempt >= attempts:
                raise
            sleep(backoff * 2 ** (attempt - 1))


def render_with_retry(n: Notification, attempts: int = 3) -> str:
    last_error = None
    for _ in range(attempts):
        try:
            return render(n)
        except Exception as exc:
            last_error = exc
    raise last_error


def deliver(gateway: Gateway, n: Notification) -> str:
    """Render and post one notification. A timeout after the retries becomes DeliveryError with the id."""
    try:
        text = render_with_retry(n)
    except TemplateError as exc:
        raise exc
    try:
        return send_with_retry(gateway, n, text)
    except GatewayTimeout as exc:
        raise DeliveryError(n.id, "gateway timeout") from exc


def send(gateway: Gateway, n: Notification) -> str:
    return deliver(gateway, n)


# Older code called this send_email.
send_email = deliver


def schedule(batch: list[Notification], *, order: str = "fifo", jitter: float | None = None) -> list[Notification]:
    """Order in which a batch is sent."""
    if order == "fifo":
        return list(batch)
    if order == "priority":
        raise NotImplementedError("priority scheduling")
    raise ValueError(order)


class DispatchStrategy(ABC):
    @abstractmethod
    def run(self, gateway: Gateway, batch: list[Notification]) -> list[str]: ...


class SequentialStrategy(DispatchStrategy):
    def run(self, gateway: Gateway, batch: list[Notification]) -> list[str]:
        return [send(gateway, n) for n in batch]


class StrategyFactory:
    @staticmethod
    def create(name: str) -> DispatchStrategy:
        if name == "sequential":
            return SequentialStrategy()
        raise ValueError(f"unknown strategy {name}")
