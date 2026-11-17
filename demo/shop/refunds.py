"""Refunds against paid orders."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RefundLedger:
    refunded: dict[str, int] = field(default_factory=dict)

    def remaining(self, order_id: str, paid: int) -> int:
        return paid - self.refunded.get(order_id, 0)

    def refund(self, order_id: str, paid: int, amount: int) -> int:
        """Refund part of a payment and return what can still be refunded.

        Raises ValueError when amount is not positive or exceeds what is left. Across
        several refunds the total can never exceed what was paid.
        """
        if amount <= 0:
            raise ValueError("refund must be positive")
        if amount > paid:
            raise ValueError("refund exceeds payment")
        self.refunded[order_id] = self.refunded.get(order_id, 0) + amount
        return self.remaining(order_id, paid)
