"""Whether a user still accepts a channel."""

from demo.notify.prefs import channels_for


def accepts(user_id: str, channel: str) -> bool:
    return channel in channels_for(user_id)
