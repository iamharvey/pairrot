"""Statement totals."""

from __future__ import annotations

from decimal import Decimal

from .db import session
from .repo import Repo


def total_balance(repo: Repo, account_ids: list[str]) -> int:
    """Sum of the balances of a customer's accounts, in cents."""
    return sum(repo.get_balance(account_id) for account_id in account_ids)


def totals_match(amounts: list[str], expected: str) -> bool:
    """True when yuan amounts such as "0.10" on a statement add up to `expected`."""
    return sum(float(amount) for amount in amounts) == float(expected)


def statement_total(amounts: list[str]) -> Decimal:
    """Exact sum of yuan amounts such as "0.10"."""
    return sum((Decimal(amount) for amount in amounts), Decimal("0"))


def account_count(path: str) -> int:
    """Number of accounts in a ledger database file."""
    with session(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
