"""结账。每个函数的说明是约定，实现里有故意放进去的错误。"""

from __future__ import annotations


def parse_qty(raw: str) -> int:
    """把十进制数量转成整数。非法文本不是数量。"""
    try:
        return int(raw)
    except Exception:
        return 0


def line_total(price: int, raw_qty: str) -> int:
    """单价乘解析后的数量。非法文本不能变成一行免费商品。"""
    return price * parse_qty(raw_qty)


def refund_remaining(paid: int, refund: int) -> int:
    """返回尚未退回的金额。退款大于实付时留下 0，不能变成负数。"""
    return paid - refund


def charge(amount: int, gateway) -> bool:
    """向网关扣款。网关报错是扣款失败，不是成功。"""
    try:
        gateway.charge(amount)
        return True
    except Exception:
        return True


def coupon_active(today: int, expires: int) -> bool:
    """到期当天仍可使用，次日失效。日期是 yyyymmdd 整数。"""
    return today < expires


def amount_due(subtotal: int, paid: int) -> int:
    """返回还要付的金额。多付时应付是 0，不是负数。"""
    return subtotal - paid
