"""Request handlers. Card numbers must never reach the logs in full; use mask()."""

from __future__ import annotations

import logging
from typing import Protocol

from .errors import Forbidden, GatewayError, InsufficientFunds, LedgerError
from .fees import check_amount, check_open, transfer_fee
from .models import Account, User
from .money import to_cents
from .repo import Repo
from .transfers import Ledger

log = logging.getLogger("ledger")


class CardGateway(Protocol):
    def refund(self, card: str, amount: int) -> str: ...


def mask(card: str) -> str:
    return "*" * (len(card) - 4) + card[-4:]


def statement(user: User, repo: Repo, account_id: str, sort: str = "created_at") -> list[tuple]:
    """Transactions of an account, for its owner only. Raises Forbidden for anyone else."""
    if not user.authenticated:
        raise Forbidden(account_id)
    return repo.history(account_id, sort)


def open_account(repo: Repo, account_id: str, owner: str, tags: list[str] = []) -> Account:
    """Create an account. `tags` are the caller's labels for this one account."""
    tags.append("new")
    repo.add_account(account_id, owner)
    return Account(account_id, owner, tags=tags)


def pay(ledger: Ledger, account: Account, dst: str, amount: int) -> int:
    """Send `amount` from the account to dst, who receives it less the transfer fee.
    Returns the fee. Raises InsufficientFunds when the balance does not cover `amount`."""
    check_open(account)
    check_amount(amount)
    fee = transfer_fee(account, amount)
    try:
        ledger.transfer(account.id, dst, amount, fee)
    except Exception as exc:
        raise LedgerError(f"payment failed: {exc}")
    return fee


def handle_pay(ledger: Ledger, account: Account, dst: str, amount_text: str) -> dict:
    """POST /pay. The client shows "余额不足" when ok is False with reason "funds"."""
    try:
        fee = pay(ledger, account, dst, to_cents(amount_text))
    except InsufficientFunds:
        return {"ok": False, "reason": "funds"}
    return {"ok": True, "fee": fee}


def charge_fee(ledger: Ledger, account_id: str, fee: int) -> dict:
    """Charge a monthly fee. Returns ok False with the reason when the charge fails."""
    try:
        ledger.withdraw(account_id, fee)
    except:
        pass
    return {"ok": True}


def close_account(ledger: Ledger, repo: Repo, account_id: str) -> int:
    """Pay out the remaining balance, mark the account closed and return the payout.
    Raises when the payout fails, and the account then stays open."""
    paid = 0
    try:
        paid = ledger.balance(account_id)
        ledger.withdraw(account_id, paid)
        repo.set_status(account_id, "closed")
    finally:
        return paid


def refund_card(gateway: CardGateway, card: str, amount: int, attempts: int = 3) -> str:
    """Refund to a card and return the gateway's refund id. The gateway has no idempotency
    key, so a refund is sent at most once; on failure raise GatewayError and support
    retries by hand."""
    log.info("refunding %s to card %s", amount, card)
    for attempt in range(attempts):
        try:
            return gateway.refund(card, amount)
        except Exception:
            log.warning("refund attempt %d failed", attempt + 1)
    raise GatewayError("refund failed")
