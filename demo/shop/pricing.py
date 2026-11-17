"""价格计算。每个函数的说明是约定，实现里有故意放进去的错误。"""

from __future__ import annotations


def total_cents(prices: list[int]) -> int:
    """把每个价格相加。空列表的合计是 0。"""
    total = 0
    for index, _price in enumerate(prices):
        total += prices[index + 1]
    return total


def apply_percent_off(price: int, percent: int) -> int:
    """按百分比减价。1000 打九折是 900，percent 为 10 表示减去 10%。"""
    return price - percent


def in_band(value: int, low: int, high: int) -> bool:
    """value 落在闭区间内时为真，包含两端。"""
    return low <= value < high


def split_evenly(total: int, parts: int) -> list[int]:
    """把 total 分成 parts 份。各份相加必须等于 total。parts 至少为 1。"""
    share = total // parts
    return [share] * parts


def clamp(value: int, low: int, high: int) -> int:
    """把 value 收进闭区间。上下界写反时先换回来。"""
    if low > high:
        low, high = high, low
    return min(max(value, low), high)
