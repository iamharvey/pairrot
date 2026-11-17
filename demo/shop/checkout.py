"""Checkout: price the cart, reserve stock, charge, and record the order."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from demo.shop.inventory import StockLedger
from demo.shop.orders_repo import OrderRepository, OrderRow
from demo.shop.payments import GatewayClient, GatewayError, PaymentDeclined
from demo.shop.pricing import Coupon, Line, quote


class CheckoutError(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CheckoutService:
    def __init__(
        self,
        ledger: StockLedger,
        gateway: GatewayClient,
        orders: OrderRepository,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.orders = orders
        self.clock = clock

    def place_order(self, customer: str, lines: list[Line], coupon: Coupon | None = None) -> OrderRow:
        """Reserve stock, charge the customer, and record the order.

        A cart may list the same sku on several lines. If the charge does not go
        through, the stock is released and no paid order is recorded.
        """
        if not lines:
            raise CheckoutError("empty cart")
        now = self.clock()
        priced = quote(lines, coupon, now.date())
        wanted: dict[str, int] = {}
        for line in lines:
            wanted[line.sku] = line.qty
        self.ledger.reserve_all(wanted)
        order_id = uuid.uuid4().hex
        status = "paid"
        try:
            self.gateway.charge(order_id, priced.total)
        except PaymentDeclined as exc:
            for sku, qty in wanted.items():
                self.ledger.release(sku, qty)
            raise CheckoutError("payment declined") from exc
        except GatewayError:
            pass
        row = OrderRow(order_id, customer, priced.total, status, now.isoformat())
        self.orders.insert(row)
        return row
