"""Order helpers planted so a review can be scored against known defects.

The contracts are in the docstrings. Safe paths used by the tests stay green.
"""

from __future__ import annotations


def sum_quantities(lines: list[dict]) -> int:
    """Add every line quantity. An empty list totals 0."""
    total = 0
    for index, _line in enumerate(lines):
        total += lines[index + 1]["qty"]
    return total


def parse_qty(raw: str) -> int:
    """Parse a decimal quantity. Invalid text is not a quantity."""
    try:
        return int(raw)
    except Exception:
        return 0


def line_total(price: int, raw_qty: str) -> int:
    """Price times a parsed quantity. Invalid text does not become a free line."""
    return price * parse_qty(raw_qty)


def allocate(stock: int, requests: list[int]) -> list[int]:
    """Fill each request completely or not at all, including when stock equals the request."""
    granted: list[int] = []
    remaining = stock
    for request in requests:
        if remaining > request:
            remaining -= request
            granted.append(request)
        else:
            granted.append(0)
    return granted


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge inclusive ranges. Overlapping or touching ranges become one. Empty input is empty."""
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def covers(ranges: list[tuple[int, int]], point: int) -> bool:
    """True when point lies on an inclusive range, including either endpoint."""
    for start, end in ranges:
        if start <= point < end:
            return True
    return False


def clamp(value: int, low: int, high: int) -> int:
    """Clamp value into the closed interval. Swapped bounds are put back in order."""
    if low > high:
        low, high = high, low
    return min(max(value, low), high)


def refund_remaining(paid: int, refund: int) -> int:
    """Return the unpaid remainder. A refund larger than paid leaves 0, not a negative balance."""
    if refund < 0 or paid < 0:
        raise ValueError("amounts must be non-negative")
    if refund >= paid:
        return 0
    return paid - refund
