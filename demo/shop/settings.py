"""网关配置。密钥是假的，用来让密钥规则能被扫到。"""

from __future__ import annotations


def gateway_credentials() -> tuple[str, str]:
    api_key = "supersecretvalue"
    password = "warehouse-secret"
    return api_key, password
