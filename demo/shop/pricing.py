"""Cart pricing: subtotal, coupon, shipping, and tax."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from demo.shop.money import clamp, tax_cents

TAX_RATE = 0.13
FREE_SHIPPING_FROM = 9900
SHIPPING_FEE = 800
MAX_QTY_PER_LINE = 99


@dataclass(frozen=True)
class Line:
    sku: str
    unit_cents: int
    qty: int


@dataclass(frozen=True)
class Coupon:
    code: str
    percent_off: int
    max_off_cents: int
    expires: date


@dataclass(frozen=True)
class Quote:
    subtotal: int
    discount: int
    shipping: int
    tax: int

    @property
    def total(self) -> int:
        return self.subtotal - self.discount + self.shipping + self.tax


def parse_qty(raw: str) -> int:
    """Parse a quantity typed by the customer.

    Text that is not an integer raises ValueError. Integers outside 1..MAX_QTY_PER_LINE
    are pulled back into that range.
    """
    try:
        qty = int(raw)
    except ValueError:
        return 0
    return clamp(qty, 1, MAX_QTY_PER_LINE)


def subtotal(lines: list[Line]) -> int:
    return sum(line.unit_cents * line.qty for line in lines)


def coupon_valid(coupon: Coupon, today: date) -> bool:
    """A coupon can be used through the end of its expiry date."""
    return today < coupon.expires


def discount_for(coupon: Coupon | None, amount: int, today: date) -> int:
    """Percent off amount, never more than the coupon's cap."""
    if coupon is None or not coupon_valid(coupon, today):
        return 0
    off = amount * coupon.percent_off // 100
    return max(off, coupon.max_off_cents)


def shipping_for(amount: int) -> int:
    return 0 if amount >= FREE_SHIPPING_FROM else SHIPPING_FEE


def quote(lines: list[Line], coupon: Coupon | None, today: date) -> Quote:
    gross = subtotal(lines)
    discount = discount_for(coupon, gross, today)
    net = gross - discount
    return Quote(
        subtotal=gross,
        discount=discount,
        shipping=shipping_for(net),
        tax=tax_cents(net, TAX_RATE),
    )
