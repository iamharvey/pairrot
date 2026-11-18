"""Amounts are integer cents everywhere inside the ledger."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def to_cents(text: str) -> int:
    """Parse a yuan amount such as "19.99" into cents (1999)."""
    return int(float(text) * 100)


def format_cents(cents: int) -> str:
    """Format cents as yuan with two decimals: 1999 -> "19.99", -50 -> "-0.50"."""
    return f"{cents // 100}.{cents % 100:02d}"


def yuan(cents: int) -> Decimal:
    """Exact yuan value of an amount in cents."""
    return (Decimal(cents) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
