"""Happy-path tests for the demo ledger."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from demo.ledger.db import connect
from demo.ledger.fees import can_withdraw, channel_fee, classify, daily_limit, fee_tier, transfer_fee
from demo.ledger.models import Account, Bill, Txn, User
from demo.ledger.reconcile import duplicates, match, missing, monthly_summary, top_movers
from demo.ledger.repo import Repo
from demo.ledger.service import handle_pay, mask, statement
from demo.ledger.statements import account_count, statement_total
from demo.ledger.tokens import save_receipt, save_statement, signature_ok, token_matches
from demo.ledger.transfers import Ledger


def _repo() -> Repo:
    repo = Repo(connect())
    repo.add_account("a1", "alice", 10_000)
    repo.add_txn("t1", "a1", "transfer", 500, "rent", "2026-11-01")
    repo.add_txn("t2", "a1", "fee", 100, "monthly fee", "2026-11-02")
    return repo


def test_repo_reads():
    repo = _repo()
    assert repo.get("a1") == ("a1", "alice", "active", 10_000)
    assert repo.get_balance("a1") == 10_000
    assert [row[0] for row in repo.find_by_owner("alice")] == ["a1"]
    assert [row[0] for row in repo.history_ordered("a1", "newest")] == ["t2", "t1"]
    assert [row[0] for row in repo.search_memo("a1", "rent")] == ["t1"]
    assert repo.balances(["a1"]) == {"a1": 10_000}


def test_ledger_moves_money():
    ledger = Ledger({"a1": 1_000, "a2": 0})
    ledger.deposit("a1", 500)
    assert ledger.withdraw("a1", 300) == 1_200
    ledger.transfer("a1", "a2", 200)
    assert ledger.balance("a2") == 200


def test_fees_and_limits():
    assert fee_tier(50_00) == "basic"
    assert daily_limit(3) == 200_000_00
    assert channel_fee("bank", 10_000) == 10
    assert transfer_fee(Account("a1", "alice"), 1_000_000) == 1_000
    assert can_withdraw(Account("a1", "alice"), 100, 1_000, 0, 1)
    assert classify(Txn("t1", "a1", "transfer", 60_000_00, "r1")) == "large"


def test_pay_from_a_regular_account():
    ledger = Ledger({"a1": 100_000, "a2": 0})
    assert handle_pay(ledger, Account("a1", "alice"), "a2", "500") == {"ok": True, "fee": 100}
    assert ledger.balance("a2") == 49_900
    assert ledger.balance("a1") == 50_000


def test_owner_reads_statement():
    repo = _repo()
    assert len(statement(User("alice"), repo, "a1")) == 2


def test_tokens_and_files(tmp_path):
    assert token_matches("abc", "abc")
    import hashlib
    import hmac

    sig = hmac.new(b"k", b"body", hashlib.sha256).hexdigest()
    assert signature_ok(b"body", b"k", sig)
    save_statement(str(tmp_path / "s.txt"), "statement")
    save_receipt(str(tmp_path / "r.txt"), "receipt")
    assert (tmp_path / "r.txt").read_text() == "receipt"


def test_statements(tmp_path):
    assert statement_total(["0.10", "0.20"]) == Decimal("0.30")
    db = str(tmp_path / "ledger.db")
    conn = connect(db)
    Repo(conn).add_account("a1", "alice")
    conn.close()
    assert account_count(db) == 1
    assert mask("6222020000001234") == "************1234"


def test_reconcile():
    assert match([("r1", 100), ("r2", 50)], [("r2", 50), ("r1", 100)]) == ["r1", "r2"]
    assert missing(["r1", "r2"], ["r1"]) == ["r2"]
    assert top_movers([("a1", 100), ("a2", -300), ("a1", 50)], k=1) == ["a2"]
    assert monthly_summary({(1, "fee"): 5})[0] == [0, 5, 0, 0]
    assert duplicates([("t1", "a1", "r"), ("t2", "a1", "r"), ("t3", "a2", "r")]) == [("t1", "t2")]


def test_bills_are_dated():
    assert Bill("b1", date(2026, 11, 1), True).paid
