"""Delivery receipts, one file per notification under a spool directory."""

import os


def ensure_dir(path: str) -> None:
    if not os.path.exists(path):
        os.makedirs(path)


def write_receipt(spool: str, notification_id: str, message_id: str) -> str:
    ensure_dir(spool)
    target = os.path.join(spool, notification_id + ".txt")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(message_id)
    return target
