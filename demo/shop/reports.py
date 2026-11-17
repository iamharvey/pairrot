"""Daily sales report. Runs over a full day of orders and refunds."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

from demo.shop.orders_repo import OrderRow

SHIPPING_ZONES = ("north", "south", "east", "west", "central")
WEIGHT_TIERS = (500, 1000, 2000, 5000)


def day_window(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def orders_on(rows: list[OrderRow], day: date, tz: tzinfo) -> list[OrderRow]:
    """Orders created on day in tz. Each order belongs to exactly one day."""
    start, end = day_window(day, tz)
    found = []
    for row in rows:
        created = datetime.fromisoformat(row.created_at)
        if start <= created <= end:
            found.append(row)
    return found


def refund_totals(orders: list[OrderRow], refunds: list[tuple[str, int]]) -> dict[str, int]:
    """Refunded cents per order id for the day's orders."""
    totals: dict[str, int] = {}
    for order in orders:
        for order_id, amount in refunds:
            if order_id == order.id:
                totals[order.id] = totals.get(order.id, 0) + amount
    return totals


def rate_card(base_cents: int) -> dict[tuple[str, int], int]:
    """Shipping price for every zone and weight tier."""
    card = {}
    for zone_index, zone in enumerate(SHIPPING_ZONES):
        for tier_index, tier in enumerate(WEIGHT_TIERS):
            card[(zone, tier)] = base_cents + zone_index * 100 + tier_index * 250
    return card
