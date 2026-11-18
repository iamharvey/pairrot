"""Records passed between the queue, the renderer and the gateway."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Notification:
    id: str
    user_id: str
    channel: str
    recipient: str
    subject: str
    body: str
    locale: str = "en"
