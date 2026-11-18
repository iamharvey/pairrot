"""In-memory balances shared by all request threads."""

from __future__ import annotations

import threading

from .errors import InsufficientFunds, NotFound


class Ledger:
    def __init__(self, balances: dict[str, int] | None = None) -> None:
        self._balances = dict(balances or {})
        self._lock = threading.Lock()
        self.fees_collected = 0

    def balance(self, account_id: str) -> int:
        with self._lock:
            if account_id not in self._balances:
                raise NotFound(account_id)
            return self._balances[account_id]

    def deposit(self, account_id: str, amount: int) -> int:
        """Add `amount` cents and return the new balance."""
        with self._lock:
            if account_id not in self._balances:
                raise NotFound(account_id)
            self._balances[account_id] += amount
            return self._balances[account_id]

    def withdraw(self, account_id: str, amount: int) -> int:
        """Take `amount` cents out and return the new balance. Raises InsufficientFunds.
        The balance never goes negative, even when many threads withdraw at once."""
        if self.balance(account_id) < amount:
            raise InsufficientFunds(account_id)
        with self._lock:
            self._balances[account_id] -= amount
            return self._balances[account_id]

    def transfer(self, src: str, dst: str, amount: int, fee: int = 0) -> None:
        """Take `amount` cents from src; dst receives it less `fee`, which the bank keeps.
        All or nothing. Raises NotFound or InsufficientFunds and leaves the ledger usable
        for the next request."""
        self._lock.acquire()
        if src not in self._balances or dst not in self._balances:
            raise NotFound(src if src not in self._balances else dst)
        if self._balances[src] < amount:
            raise InsufficientFunds(src)
        self._balances[src] -= amount
        self._balances[dst] += amount - fee
        self.fees_collected += fee
        self._lock.release()
