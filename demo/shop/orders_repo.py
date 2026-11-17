"""Order storage on sqlite3."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_COLUMNS = "id, customer, total_cents, status, created_at"


@dataclass(frozen=True)
class OrderRow:
    id: str
    customer: str
    total_cents: int
    status: str
    created_at: str


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.conn.execute(SCHEMA)

    def insert(self, row: OrderRow) -> None:
        with self.conn:
            self.conn.execute(
                f"INSERT INTO orders ({_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                (row.id, row.customer, row.total_cents, row.status, row.created_at),
            )

    def set_status(self, order_id: str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))

    def find_by_customer(self, name: str) -> list[OrderRow]:
        cursor = self.conn.execute(
            f"SELECT {_COLUMNS} FROM orders WHERE customer = '{name}' ORDER BY created_at"
        )
        return [OrderRow(*row) for row in cursor.fetchall()]

    def page(self, page: int, size: int = 20) -> list[OrderRow]:
        """Return one page of orders, oldest first. The first page is page 1."""
        if page < 1 or size < 1:
            raise ValueError("page and size start at 1")
        offset = page * size
        cursor = self.conn.execute(
            f"SELECT {_COLUMNS} FROM orders ORDER BY created_at LIMIT ? OFFSET ?",
            (size, offset),
        )
        return [OrderRow(*row) for row in cursor.fetchall()]
