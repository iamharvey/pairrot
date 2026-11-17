"""Happy-path tests for the demo shop."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from demo.shop.checkout import CheckoutService
from demo.shop.inventory import StockLedger
from demo.shop.money import allocate_proportional, clamp, format_cents, split_evenly, tax_cents
from demo.shop.orders_repo import OrderRepository, OrderRow
from demo.shop.payments import GatewayClient
from demo.shop.pricing import Coupon, Line, parse_qty, quote
from demo.shop.refunds import RefundLedger
from demo.shop.reports import orders_on, rate_card, refund_totals
from demo.shop.webhooks import parse_event, sign, verify

NOON = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


class _Gateway(GatewayClient):
    def __init__(self) -> None:
        super().__init__("https://pay.example.test", "test-key")
        self.charged: list[tuple[str, int]] = []

    def _post(self, path: str, body: dict, headers: dict[str, str]) -> dict:
        self.charged.append((body["order"], body["amount"]))
        return {"charge_id": "ch_1"}


def test_money_helpers():
    assert format_cents(12345) == "CNY 123.45"
    assert clamp(0, 10, 1) == 1
    assert split_evenly(100, 4) == [25, 25, 25, 25]
    assert sum(allocate_proportional(100, [1, 1, 1])) == 100
    assert tax_cents(1000, 0.13) == 130


def test_quote_with_a_coupon_at_its_cap():
    coupon = Coupon("TEN", 10, 1000, date(2026, 12, 31))
    priced = quote([Line("A", 5000, 2)], coupon, date(2026, 11, 17))
    assert priced.discount == 1000
    assert priced.shipping == 800
    assert priced.total == 10000 - 1000 + 800 + 1170


def test_parse_qty_keeps_numbers_in_range():
    assert parse_qty("3") == 3
    assert parse_qty("500") == 99


def test_ledger_reserves_and_releases():
    ledger = StockLedger({"A": 10})
    ledger.reserve("A", 3)
    ledger.release("A", 1)
    assert ledger.available("A") == 8


def test_checkout_records_a_paid_order():
    ledger = StockLedger({"A": 10, "B": 10})
    orders = OrderRepository(sqlite3.connect(":memory:"))
    gateway = _Gateway()
    service = CheckoutService(ledger, gateway, orders, clock=lambda: NOON)
    row = service.place_order("alice", [Line("A", 1000, 2), Line("B", 500, 1)])
    assert row.status == "paid"
    assert gateway.charged == [(row.id, row.total_cents)]
    assert ledger.available("A") == 8
    assert orders.find_by_customer("alice") == [row]


def test_refund_part_of_a_payment():
    ledger = RefundLedger()
    assert ledger.refund("o1", 1000, 300) == 700


def test_report_helpers():
    row = OrderRow("o1", "alice", 1000, "paid", NOON.isoformat())
    assert orders_on([row], date(2026, 11, 17), timezone.utc) == [row]
    assert refund_totals([row], [("o1", 200), ("o2", 50)]) == {"o1": 200}
    assert len(rate_card(500)) == 20


def test_webhook_signature():
    body = b'{"type":"charge.succeeded"}'
    secret = b"webhook-test"
    assert verify(secret, body, sign(secret, body))
    assert not verify(secret, body, "0" * 64)
    assert parse_event(secret, body, sign(secret, body))["type"] == "charge.succeeded"
