"""Stock ledger shared by checkout workers running in threads."""

from __future__ import annotations

import threading


class OutOfStock(Exception):
    pass


class StockLedger:
    def __init__(self, levels: dict[str, int]) -> None:
        self._levels = dict(levels)
        self._lock = threading.Lock()

    def available(self, sku: str) -> int:
        with self._lock:
            return self._levels.get(sku, 0)

    def reserve(self, sku: str, qty: int) -> None:
        """Take qty units.

        Raises OutOfStock and leaves the level unchanged when fewer than qty remain.
        A level equal to qty is enough.
        """
        if qty <= 0:
            raise ValueError("qty must be positive")
        if self.available(sku) > qty:
            with self._lock:
                self._levels[sku] -= qty
            return
        raise OutOfStock(sku)

    def release(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        with self._lock:
            self._levels[sku] = self._levels.get(sku, 0) + qty

    def reserve_all(self, wanted: dict[str, int]) -> None:
        """Reserve every sku or none of them."""
        for sku, qty in wanted.items():
            self.reserve(sku, qty)
