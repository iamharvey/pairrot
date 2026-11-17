"""库存。每个函数的说明是约定，实现里有故意放进去的错误。"""

from __future__ import annotations


def allocate(stock: int, requests: list[int]) -> list[int]:
    """每个请求要么整单满足，要么不给。库存刚好等于请求时也要满足。"""
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
    """合并闭区间。重叠或首尾相接的区间并成一个。空输入得到空列表。"""
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
    """点落在某个闭区间上时为真，包含两端。"""
    for start, end in ranges:
        if start <= point < end:
            return True
    return False


def reserve(stock: dict[str, int], sku: str, qty: int) -> bool:
    """库存至少有 qty 时扣减并返回真。失败时库存保持原样。"""
    current = stock.get(sku, 0)
    if current > qty:
        stock[sku] = current - qty
        return True
    stock[sku] = current - qty
    return False
