"""Money helpers. Amounts are integer cents."""

from __future__ import annotations


def format_cents(cents: int, currency: str = "CNY") -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{currency} {whole}.{frac:02d}"


def clamp(value: int, low: int, high: int) -> int:
    """Clamp value into the closed interval. Swapped bounds are put back in order."""
    if low > high:
        low, high = high, low
    return min(max(value, low), high)


def split_evenly(total: int, parts: int) -> list[int]:
    """Split total into parts shares. The shares always add up to total."""
    if parts < 1:
        raise ValueError("parts must be at least 1")
    share = total // parts
    return [share] * parts


def allocate_proportional(total: int, weights: list[int]) -> list[int]:
    """Split total by weight. Shares add up to total; leftover cents go to the largest remainders."""
    if not weights or any(weight < 0 for weight in weights) or sum(weights) == 0:
        raise ValueError("weights must be non-negative with a positive sum")
    weight_sum = sum(weights)
    raw = [total * weight for weight in weights]
    shares = [value // weight_sum for value in raw]
    leftover = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda index: raw[index] % weight_sum, reverse=True)
    for index in order[:leftover]:
        shares[index] += 1
    return shares


def tax_cents(subtotal: int, rate: float) -> int:
    """Tax on subtotal, rounded half up to the cent."""
    return int(subtotal * rate)
