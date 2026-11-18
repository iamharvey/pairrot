"""Short-lived cache for preference lookups.

Users change their preferences at any time, so an entry is reused for at most ttl seconds.
"""

import time


class TtlCache:
    def __init__(self, ttl: float, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._items: dict = {}

    def get(self, key, load):
        """Cached value for key, or load(key) when it is missing or older than ttl."""
        now = self.clock()
        hit = self._items.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
        value = load(key)
        self._items[key] = (value, now + self.ttl)
        return value
