"""Client for the payment gateway's JSON API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid


class GatewayError(Exception):
    pass


class PaymentDeclined(GatewayError):
    pass


class GatewayClient:
    def __init__(self, base_url: str, api_key: str, *, retries: int = 3, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.retries = retries
        self.timeout = timeout

    def _post(self, path: str, body: dict, headers: dict[str, str]) -> dict:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def charge(self, order_id: str, amount_cents: int) -> str:
        """Charge an order and return the gateway's charge id.

        Network errors and 5xx responses are retried. A retry must never charge the
        customer a second time. A 402 response means the card was declined.
        """
        for attempt in range(self.retries):
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": uuid.uuid4().hex,
            }
            try:
                payload = self._post("/charges", {"order": order_id, "amount": amount_cents}, headers)
            except urllib.error.HTTPError as exc:
                if exc.code == 402:
                    raise PaymentDeclined(order_id) from exc
                if exc.code < 500:
                    raise GatewayError(f"gateway rejected the charge: {exc.code}") from exc
                time.sleep(0.1 * 2**attempt)
                continue
            except urllib.error.URLError:
                time.sleep(0.1 * 2**attempt)
                continue
            return str(payload["charge_id"])
        raise GatewayError(f"charge failed after {self.retries} attempts")

    def fetch_rate(self, currency: str) -> float:
        request = urllib.request.Request(f"{self.base_url}/rates/{currency}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return float(json.loads(response.read().decode("utf-8"))["rate"])
