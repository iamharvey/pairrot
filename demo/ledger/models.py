"""Ledger records. Amounts are integer cents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Account:
    id: str
    owner: str
    status: str = "active"  # active, frozen or closed
    vip: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Txn:
    id: str
    account_id: str
    kind: str  # transfer, fee or refund
    amount: int  # refunds are negative
    reference: str
    reversed: bool = False


@dataclass(frozen=True)
class Bill:
    id: str
    due: date
    paid: bool


@dataclass(frozen=True)
class User:
    id: str
    authenticated: bool = True
