"""End-of-day reconciliation. A day has about 100k bank statement lines and 100k ledger
transactions, and the job checks all 200k accounts."""

from __future__ import annotations

from collections import defaultdict

from .repo import Repo

KINDS = ("transfer", "fee", "refund", "interest")


def match(lines: list[tuple[str, int]], txns: list[tuple[str, int]]) -> list[str]:
    """References that appear on both sides with the same amount."""
    matched = []
    for ref, amount in lines:
        for txn_ref, txn_amount in txns:
            if ref == txn_ref and amount == txn_amount:
                matched.append(ref)
                break
    return matched


def missing(statement_refs: list[str], ledger_refs: list[str]) -> list[str]:
    """Statement references with no ledger transaction."""
    return [ref for ref in statement_refs if ref not in ledger_refs]


def low_balances(repo: Repo, account_ids: list[str], threshold: int) -> list[str]:
    """Accounts whose balance is below `threshold` cents."""
    low = []
    for account_id in account_ids:
        _, _, _, balance = repo.get(account_id)
        if balance < threshold:
            low.append(account_id)
    return low


def top_movers(txns: list[tuple[str, int]], k: int = 10) -> list[str]:
    """The k accounts with the largest total movement over the day."""
    totals: dict[str, int] = defaultdict(int)
    top: list[str] = []
    for account_id, amount in txns:
        totals[account_id] += abs(amount)
        top = sorted(totals, key=totals.__getitem__, reverse=True)[:k]
    return top


def monthly_summary(totals: dict[tuple[int, str], int]) -> list[list[int]]:
    """A 12 x 4 table of totals by month and kind."""
    table = []
    for month in range(1, 13):
        row = []
        for kind in KINDS:
            row.append(totals.get((month, kind), 0))
        table.append(row)
    return table


def duplicates(txns: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Pairs of transaction ids, given as (id, account, reference), that share an account
    and a reference."""
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for txn_id, account_id, ref in txns:
        groups[(account_id, ref)].append(txn_id)
    pairs = []
    for ids in groups.values():
        for i, first in enumerate(ids):
            for second in ids[i + 1 :]:
                pairs.append((first, second))
    return pairs
