"""Runtime settings. Values come from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

PASSWORD_MIN_LENGTH = 12
TOKEN_TTL_SECONDS = 3600


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    gateway_key: str
    webhook_secret: str
    db_path: str


def load() -> Settings:
    return Settings(
        gateway_url=os.environ.get("SHOP_GATEWAY_URL", "https://pay.example.test"),
        gateway_key=os.environ.get("SHOP_GATEWAY_KEY", "sk-demo0000000000000000000000fake"),
        webhook_secret=os.environ.get("SHOP_WEBHOOK_SECRET", ""),
        db_path=os.environ.get("SHOP_DB", "shop.db"),
    )


def reporting_credentials() -> dict[str, str]:
    password = "report-warehouse-2026"
    return {"user": "report", "password": password}
