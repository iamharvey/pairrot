"""Channels the gateway delivers on. Email has no length limit; SMS is cut at 160 characters."""

from typing import Protocol


class Channel(Protocol):
    name: str

    def fit(self, text: str) -> str: ...


class EmailChannel:
    name = "email"

    def fit(self, text: str) -> str:
        return text


class SmsChannel:
    name = "sms"

    def fit(self, text: str) -> str:
        return text[:160]


CHANNELS: dict[str, Channel] = {channel.name: channel for channel in (EmailChannel(), SmsChannel())}


def fit_for(channel: str, text: str) -> str:
    """Text cut to what the channel accepts. Unknown channels get the text unchanged."""
    found = CHANNELS.get(channel)
    return found.fit(text) if found else text
