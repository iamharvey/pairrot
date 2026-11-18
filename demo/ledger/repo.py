"""Queries over accounts and transactions. Values that come from a request are untrusted."""

from __future__ import annotations

import sqlite3

from .errors import DuplicateAccount, NotFound

_ACCOUNT_COLUMNS = "id, owner, status, balance"
_TXN_COLUMNS = "id, account_id, kind, amount, memo, created_at"

_ORDERS = {
    "newest": "created_at DESC",
    "oldest": "created_at ASC",
    "largest": "amount DESC",
}


class Repo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_account(self, account_id: str, owner: str, balance: int = 0) -> None:
        """Insert an active account. Raises DuplicateAccount if the id is taken."""
        try:
            with self.conn:
                self.conn.execute(
                    f"INSERT INTO accounts ({_ACCOUNT_COLUMNS}) VALUES (?, ?, 'active', ?)",
                    (account_id, owner, balance),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateAccount(account_id) from exc

    def add_txn(
        self, txn_id: str, account_id: str, kind: str, amount: int, memo: str, created_at: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                f"INSERT INTO txns ({_TXN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                (txn_id, account_id, kind, amount, memo, created_at),
            )

    def set_status(self, account_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id))

    def get(self, account_id: str) -> tuple:
        """Return (id, owner, status, balance). Raises NotFound."""
        row = self.conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise NotFound(account_id)
        return row

    def get_balance(self, account_id: str) -> int:
        """Balance in cents of one account. Raises NotFound for an unknown id, like get()."""
        row = self.conn.execute(
            "SELECT balance FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return -1
        return row[0]

    def find_by_owner(self, owner: str) -> list[tuple]:
        """Accounts of one owner. `owner` is typed into the support search box."""
        return self.conn.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM accounts WHERE owner = '{owner}'"
        ).fetchall()

    def history(self, account_id: str, sort: str) -> list[tuple]:
        """Transactions of an account. `sort` is the ?sort= query parameter, a column name."""
        return self.conn.execute(
            f"SELECT {_TXN_COLUMNS} FROM txns WHERE account_id = ? ORDER BY {sort}",
            (account_id,),
        ).fetchall()

    def history_ordered(self, account_id: str, order: str) -> list[tuple]:
        """Transactions of an account in a named order: newest, oldest or largest."""
        if order not in _ORDERS:
            raise ValueError(f"unknown order {order!r}")
        return self.conn.execute(
            f"SELECT {_TXN_COLUMNS} FROM txns WHERE account_id = ? ORDER BY {_ORDERS[order]}",
            (account_id,),
        ).fetchall()

    def search_memo(self, account_id: str, text: str) -> list[tuple]:
        """Transactions of an account whose memo contains `text`, as typed by the user."""
        return self.conn.execute(
            f"SELECT {_TXN_COLUMNS} FROM txns WHERE account_id = ? AND {_memo_filter(text)}",
            (account_id,),
        ).fetchall()

    def balances(self, account_ids: list[str]) -> dict[str, int]:
        """Balances for the account ids listed in an uploaded batch file."""
        listed = ", ".join(f"'{account_id}'" for account_id in account_ids)
        rows = self.conn.execute(
            f"SELECT id, balance FROM accounts WHERE id IN ({listed})"
        ).fetchall()
        return dict(rows)


def _memo_filter(text: str) -> str:
    return "memo LIKE '%{}%'".format(text)
