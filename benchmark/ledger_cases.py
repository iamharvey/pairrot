"""Answer key for demo/ledger, with an executable check for every row.

usage (from the repo root):
  python benchmark/ledger_cases.py          run every check
  python benchmark/ledger_cases.py --key    also write benchmark/ledger_answer_key.jsonl
  python benchmark/ledger_cases.py --base <sha>   confirm no defect anchor exists at <sha>

A defect check passes when the defect shows up. A clean check passes when the code is right.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(os.environ.get("LEDGER_ROOT") or Path(__file__).resolve().parents[1])
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from demo.ledger import fees, reconcile, service, statements, tokens, transfers  # noqa: E402
from demo.ledger.db import connect  # noqa: E402
from demo.ledger.errors import (  # noqa: E402
    AccountFrozen,
    DuplicateAccount,
    GatewayError,
    InsufficientFunds,
    LedgerError,
    NotFound,
)
from demo.ledger.models import Account, Bill, Txn, User  # noqa: E402
from demo.ledger.money import format_cents, to_cents  # noqa: E402
from demo.ledger.repo import Repo  # noqa: E402

LABELS = {
    "logic_error": "逻辑错误",
    "error_handling": "错误处理缺失",
    "concurrency": "并发与时序",
    "security": "安全与权限",
    "performance": "性能后果",
    "data_consistency": "数据一致性",
}
IN_SCOPE = {"logic_error", "error_handling"}


@dataclass
class Case:
    id: str
    kind: str  # defect, clean or preexisting
    scenario: str
    file: str
    function: str
    start: str
    end: str
    category: str
    cwe: str
    title: str
    trigger: str
    consequence: str
    check: object


CASES: list[Case] = []


def case(id, kind, scenario, file, function, start, end, category, cwe, title, trigger="", consequence=""):
    def wrap(fn):
        CASES.append(Case(id, kind, scenario, file, function, start, end or start, category, cwe,
                          title, trigger, consequence or ("报这里算误报" if kind == "clean" else ""), fn))
        return fn
    return wrap


def _repo() -> Repo:
    repo = Repo(connect())
    repo.add_account("a1", "alice", 10_000)
    repo.add_account("a2", "bob", 20_000)
    repo.add_txn("t1", "a1", "transfer", 500, "rent", "2026-11-01")
    repo.add_txn("t2", "a2", "transfer", 900, "salary", "2026-11-01")
    return repo


class Counted(str):
    hits = 0

    def __eq__(self, other):
        Counted.hits += 1
        return str.__eq__(self, other)

    __hash__ = str.__hash__


def _hangs(fn, seconds=0.5) -> bool:
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(seconds)
    return t.is_alive()


def _race(fn, n=8):
    threads = [threading.Thread(target=fn) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


R, T, K, F, S, RC, SV, MO = (
    "demo/ledger/repo.py", "demo/ledger/transfers.py", "demo/ledger/tokens.py", "demo/ledger/fees.py",
    "demo/ledger/statements.py", "demo/ledger/reconcile.py", "demo/ledger/service.py", "demo/ledger/money.py",
)

# ---- SQL 注入 -------------------------------------------------------------


@case("LED-D01", "defect", "sql_injection", R, "Repo.find_by_owner", "WHERE owner = '{owner}'", "",
      "security", "CWE-89", "客服搜索框的户主名直接拼进 SQL",
      "owner = \"x' OR '1'='1\"", "返回所有人的账户")
def _():
    return len(_repo().find_by_owner("x' OR '1'='1")) == 2


@case("LED-D02", "defect", "sql_injection", R, "Repo.history", "ORDER BY {sort}", "",
      "security", "CWE-89", "?sort= 参数当列名拼进 ORDER BY",
      "sort = \"(SELECT CASE WHEN substr(owner,1,1)='b' THEN 1 ELSE 0 END FROM accounts WHERE id='a2')\"",
      "可以执行任意子查询，按排序结果逐位猜出别人的数据；service.statement 把请求参数原样传进来")
def _():
    _repo().history("a1", "(SELECT CASE WHEN substr(owner,1,1)='b' THEN 1 ELSE 0 END FROM accounts WHERE id='a2')")
    return True


@case("LED-D03", "defect", "sql_injection", R, "_memo_filter", "memo LIKE '%{}%'", "",
      "security", "CWE-89", "备注搜索经 _memo_filter 拼进 SQL（跨函数）",
      "search_memo('a1', \"%' OR account_id != '\")", "返回别人账户的流水")
def _():
    rows = _repo().search_memo("a1", "%' OR account_id != '")
    return any(row[1] == "a2" for row in rows)


@case("LED-D04", "defect", "sql_injection", R, "Repo.balances", "listed = \", \".join", "WHERE id IN ({listed})",
      "security", "CWE-89", "批量文件里的账户 id 拼成 IN 列表",
      "account_ids = [\"x') OR ('1'='1\"]", "返回全部账户余额")
def _():
    return len(_repo().balances(["x') OR ('1'='1"])) == 2


@case("LED-C01", "clean", "sql_injection", R, "Repo.get", "WHERE id = ?", "",
      "security", "", "参数化查询")
def _():
    try:
        _repo().get("a1' OR '1'='1")
    except NotFound:
        return True
    return False


@case("LED-C02", "clean", "sql_injection", R, "Repo.history_ordered", "if order not in _ORDERS", "ORDER BY {_ORDERS[order]}",
      "security", "", "排序名先经白名单映射再拼 SQL")
def _():
    try:
        _repo().history_ordered("a1", "amount; DROP TABLE txns")
    except ValueError:
        return True
    return False


@case("LED-C03", "clean", "sql_injection", R, "Repo.add_account", "INSERT INTO accounts ({_ACCOUNT_COLUMNS})", "",
      "security", "", "f-string 只拼模块常量列名，值走参数")
def _():
    repo = _repo()
    repo.add_account("a3", "o'brien")
    return repo.get("a3")[1] == "o'brien"


# ---- 嵌套循环与复杂度 -------------------------------------------------------


@case("LED-D05", "defect", "nested_loop", RC, "match", "for ref, amount in lines:", "if ref == txn_ref",
      "performance", "CWE-407", "对账两两比对，O(n·m)",
      "10 万条对账单 × 10 万条流水", "约 10^10 次比较，日终任务跑不完")
def _():
    n = 200
    lines = [(Counted(f"r{i}"), 1) for i in range(n)]
    txns = [(f"r{i}", 1) for i in reversed(range(n))]
    Counted.hits = 0
    reconcile.match(lines, txns)
    return Counted.hits > n * n / 3


@case("LED-D06", "defect", "nested_loop", RC, "missing", "ref not in ledger_refs", "",
      "performance", "CWE-407", "循环里对 list 做 in，隐性平方",
      "10 万条对账单引用 vs 10 万条流水引用", "约 10^10 次比较")
def _():
    n = 200
    refs = [Counted(f"x{i}") for i in range(n)]
    Counted.hits = 0
    reconcile.missing([f"y{i}" for i in range(n)], refs)
    return Counted.hits >= n * n


@case("LED-D07", "defect", "nested_loop", RC, "low_balances", "for account_id in account_ids:", "repo.get(account_id)",
      "performance", "CWE-1073", "循环里逐个查库（N+1）",
      "20 万个账户", "20 万次查询；应一次批量查询")
def _():
    class Stub:
        calls = 0

        def get(self, account_id):
            Stub.calls += 1
            return (account_id, "o", "active", 0)

    reconcile.low_balances(Stub(), [f"a{i}" for i in range(50)], 10)
    return Stub.calls == 50


@case("LED-D08", "defect", "nested_loop", RC, "top_movers", "for account_id, amount in txns:", "top = sorted(",
      "performance", "CWE-407", "每条流水都把全部账户重新排序一次",
      "10 万条流水、几万个账户", "O(n·m log m)；只需循环结束后排一次")
def _():
    calls = []
    real = sorted
    reconcile.sorted = lambda *a, **kw: (calls.append(1), real(*a, **kw))[1]
    try:
        reconcile.top_movers([(f"a{i % 7}", i) for i in range(40)], k=3)
    finally:
        del reconcile.sorted
    return len(calls) == 40


@case("LED-C04", "clean", "nested_loop", RC, "monthly_summary", "for month in range(1, 13):", "row.append(",
      "performance", "", "12 × 4 的固定双重循环")
def _():
    table = reconcile.monthly_summary({(12, "interest"): 7})
    return len(table) == 12 and all(len(r) == 4 for r in table) and table[11][3] == 7


@case("LED-C05", "clean", "nested_loop", RC, "duplicates", "for ids in groups.values():", "pairs.append(",
      "performance", "", "先按 (账户, 引用) 分组，组内才两两配对")
def _():
    got = reconcile.duplicates([("t1", "a", "r"), ("t2", "a", "r"), ("t3", "b", "r"), ("t4", "a", "s")])
    return got == [("t1", "t2")]


# ---- 时序不安全 ---------------------------------------------------------------


@case("LED-D09", "defect", "timing", T, "Ledger.withdraw", "if self.balance(account_id) < amount:", "self._balances[account_id] -= amount",
      "concurrency", "CWE-367", "余额检查和扣款不在同一把锁里",
      "两个线程同时从 100 分的账户各取 100 分", "余额变成 -100，违反“永不为负”")
def _():
    class Slow(transfers.Ledger):
        def balance(self, account_id):
            value = super().balance(account_id)
            time.sleep(0.05)
            return value

    ledger = Slow({"a": 100})
    _race(lambda: ledger.withdraw("a", 100), n=2)
    return ledger._balances["a"] < 0


@case("LED-D10", "defect", "timing", K, "token_matches", "return given == expected", "",
      "security", "CWE-208", "API token 用 == 比较",
      "攻击者按响应时间逐字节猜 token", "比较耗时泄露 token 前缀；应使用 hmac.compare_digest")
def _():
    return "compare_digest" not in inspect.getsource(tokens.token_matches)


@case("LED-D11", "defect", "timing", K, "rate_client", "if _client is None:", "_client = RateClient()",
      "concurrency", "CWE-543", "进程级单例懒加载不加锁",
      "多个请求线程同时首次调用 rate_client()", "开出多个会话，违反“每进程一个会话”")
def _():
    real = tokens.RateClient

    class Slow(real):
        def __init__(self):
            time.sleep(0.05)
            super().__init__()

    tokens.RateClient, tokens._client, Slow.opened = Slow, None, 0
    try:
        _race(tokens.rate_client)
    finally:
        tokens.RateClient, tokens._client = real, None
    return Slow.opened > 1


@case("LED-D12", "defect", "timing", K, "save_statement", "if os.path.exists(path):", "with open(path, \"w\"",
      "concurrency", "CWE-367", "先 exists 再 open(\"w\")，检查和使用之间有窗口",
      "共享目录里另一个任务在检查之后写入同名文件", "对方的文件被覆盖，违反“不覆盖”")
def _():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.txt")
        real = tokens.os.path.exists

        def racing(p):
            answer = real(p)
            Path(p).write_text("other job")
            return answer

        tokens.os.path.exists = racing
        try:
            tokens.save_statement(path, "mine")
        finally:
            tokens.os.path.exists = real
        return Path(path).read_text() == "mine"


@case("LED-C06", "clean", "timing", K, "signature_ok", "hmac.compare_digest(expected.encode()", "",
      "security", "", "签名按字节用 compare_digest 比较")
def _():
    sig = hmac.new(b"k", b"b", hashlib.sha256).hexdigest()
    return (tokens.signature_ok(b"b", b"k", sig)
            and not tokens.signature_ok(b"b", b"k", "0" * 64)
            and not tokens.signature_ok(b"b", b"k", "签名"))


@case("LED-C07", "clean", "timing", T, "Ledger.deposit", "with self._lock:", "self._balances[account_id] += amount",
      "concurrency", "", "读改写都在锁内")
def _():
    ledger = transfers.Ledger({"a": 0})
    _race(lambda: [ledger.deposit("a", 1) for _ in range(2000)])
    return ledger.balance("a") == 16000


@case("LED-C08", "clean", "timing", K, "save_receipt", "os.O_CREAT | os.O_EXCL", "",
      "concurrency", "", "O_CREAT|O_EXCL 原子创建")
def _():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "r.txt")
        Path(path).write_text("other")
        try:
            tokens.save_receipt(path, "mine")
        except FileExistsError:
            return Path(path).read_text() == "other"
        return False


# ---- 糟糕的嵌套 -------------------------------------------------------------


@case("LED-D13", "defect", "bad_nesting", F, "transfer_fee", "if account.vip:", "fee = min(amount",
      "logic_error", "CWE-483", "else 挂在外层 if 上，VIP 小额免了手续费",
      "transfer_fee(vip, 100_00)", "约定收 1 元，实际 0")
def _():
    return fees.transfer_fee(Account("v", "o", vip=True), 100_00) == 0


@case("LED-D14", "defect", "bad_nesting", F, "first_overdue", "for bill in bills:", "return None",
      "logic_error", "CWE-483", "return None 缩进在循环里，只看第一张账单",
      "[已付账单, 逾期未付账单]", "返回 None，应返回第二张")
def _():
    bills = [Bill("b1", date(2026, 1, 1), True), Bill("b2", date(2026, 1, 1), False)]
    return fees.first_overdue(bills, date(2026, 11, 1)) is None


@case("LED-D15", "defect", "bad_nesting", F, "classify", "if txn.kind == \"refund\":", "return \"reversed\"",
      "logic_error", "CWE-483", "退款分支缺 return，掉进大额/普通判断",
      "classify(未冲正的退款)", "返回 'normal' 或 'large'，应为 'refund'")
def _():
    return fees.classify(Txn("t", "a", "refund", -500, "r")) == "normal"


@case("LED-C09", "clean", "bad_nesting", F, "can_withdraw", "if account.status == \"active\":", "return False",
      "logic_error", "", "四层嵌套但逻辑正确；报重构建议算噪音")
def _():
    a = Account("a", "o")
    return (fees.can_withdraw(a, 100, 100, 0, 1)
            and not fees.can_withdraw(a, 0, 100, 0, 1)
            and not fees.can_withdraw(a, 101, 100, 0, 1)
            and not fees.can_withdraw(a, 100, 10**9, 5_000_00, 1)
            and not fees.can_withdraw(Account("a", "o", status="frozen"), 1, 100, 0, 1))


# ---- if-else 不可达 ---------------------------------------------------------


@case("LED-D16", "defect", "unreachable", F, "fee_tier", "if amount > 100_00:", "return \"premium\"",
      "logic_error", "CWE-561", "分档顺序写反，premium 分支不可达",
      "fee_tier(2000_00)", "返回 'standard'，应为 'premium'")
def _():
    return fees.fee_tier(2000_00) == "standard"


@case("LED-D17", "defect", "unreachable", F, "check_open", "if account.status in (\"closed\", \"frozen\"):", "raise AccountClosed",
      "logic_error", "CWE-561", "closed 已被上一个分支吃掉，AccountClosed 不可达",
      "check_open(已销户账户)", "抛 AccountFrozen，应抛 AccountClosed")
def _():
    try:
        fees.check_open(Account("a", "o", status="closed"))
    except AccountFrozen:
        return True
    return False


@case("LED-D18", "defect", "unreachable", F, "check_amount", "if amount <= 0 and amount > MAX_TRANSFER:", "",
      "logic_error", "CWE-570", "条件恒假（应为 or），校验永不触发",
      "check_amount(-5)", "负数金额通过校验")
def _():
    fees.check_amount(-5)
    fees.check_amount(fees.MAX_TRANSFER + 1)
    return True


@case("LED-D19", "defect", "unreachable", F, "waive_fee", "return 0", "audit.append(",
      "logic_error", "CWE-561", "审计日志写在 return 之后，永远不执行",
      "waive_fee(vip, ...)", "减免不留审计记录")
def _():
    audit: list[str] = []
    fees.waive_fee(Account("v", "o", vip=True), 100, audit)
    return audit == []


@case("LED-C10", "clean", "unreachable", F, "daily_limit", "if level >= 3:", "return 5_000_00",
      "logic_error", "", "分档从高到低，每档都可达")
def _():
    return [fees.daily_limit(x) for x in (4, 3, 2, 1)] == [200_000_00, 200_000_00, 50_000_00, 5_000_00]


@case("LED-C11", "clean", "unreachable", F, "channel_fee", "else:", "raise ValueError(f\"unknown channel",
      "logic_error", "", "对请求里的未知渠道兜底抛错，可达")
def _():
    try:
        fees.channel_fee("cash", 1)
    except ValueError:
        return True
    return False


# ---- 错误处理设计缺陷 ---------------------------------------------------------


@case("LED-D20", "defect", "error_handling", R, "Repo.get_balance", "if row is None:", "return -1",
      "error_handling", "CWE-394", "查不到账户返回 -1 而不是抛 NotFound",
      "statements.total_balance(repo, ['a1', '不存在'])", "合计少 1 分且不报错；约定应抛 NotFound")
def _():
    return statements.total_balance(_repo(), ["a1", "nope"]) == 10_000 - 1


@case("LED-D21", "defect", "error_handling", T, "Ledger.transfer", "self._lock.acquire()", "self._lock.release()",
      "error_handling", "CWE-667", "抛异常时锁不释放",
      "余额不足的一次 transfer 之后再调用任何方法", "整个 Ledger 永久死锁")
def _():
    ledger = transfers.Ledger({"a": 0, "b": 0})
    try:
        ledger.transfer("a", "b", 1)
    except InsufficientFunds:
        pass
    return _hangs(lambda: ledger.balance("a"))


@case("LED-D22", "defect", "error_handling", SV, "pay", "except Exception as exc:", "raise LedgerError(",
      "error_handling", "CWE-755", "InsufficientFunds 被包成 LedgerError",
      "handle_pay 余额不足", "调用方的 except InsufficientFunds 接不住，返回 500 而不是“余额不足”")
def _():
    try:
        service.handle_pay(transfers.Ledger({"a": 0, "b": 0}), Account("a", "o"), "b", "5")
    except LedgerError as exc:
        return not isinstance(exc, InsufficientFunds)
    return False


@case("LED-D23", "defect", "error_handling", SV, "charge_fee", "except:", "return {\"ok\": True}",
      "error_handling", "CWE-390", "裸 except 吞掉失败并返回成功",
      "charge_fee 余额不足", "返回 ok True，费用没扣到")
def _():
    return service.charge_fee(transfers.Ledger({"a": 0}), "a", 100) == {"ok": True}


@case("LED-D24", "defect", "error_handling", SV, "close_account", "finally:", "return paid",
      "error_handling", "CWE-584", "finally 里 return 吞掉异常",
      "close_account 遇到 NotFound 或标记失败", "返回 0，调用方以为销户成功")
def _():
    return service.close_account(transfers.Ledger({}), _repo(), "nope") == 0


@case("LED-D25", "defect", "error_handling", SV, "refund_card", "for attempt in range(attempts):", "log.warning(",
      "error_handling", "CWE-755", "非幂等退款遇到任何异常都重试",
      "网关已退款但响应超时", "同一笔退款被发出多次")
def _():
    class Gateway:
        sent = 0

        def refund(self, card, amount):
            Gateway.sent += 1
            if Gateway.sent == 1:
                raise TimeoutError("read timed out")
            return "rf_1"

    service.refund_card(Gateway(), "6222020000001234", 100)
    return Gateway.sent == 2


@case("LED-C12", "clean", "error_handling", R, "Repo.add_account", "except sqlite3.IntegrityError as exc:", "raise DuplicateAccount(account_id) from exc",
      "error_handling", "", "只捕获具体异常，按约定转换并保留原因")
def _():
    repo = _repo()
    try:
        repo.add_account("a1", "x")
    except DuplicateAccount as exc:
        return exc.__cause__ is not None
    return False


@case("LED-C13", "clean", "error_handling", S, "account_count", "with session(path) as conn:", "",
      "error_handling", "", "用上下文管理器管理连接")
def _():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "l.db")
        connect(db).close()
        return statements.account_count(db) == 0


# ---- 其他 -------------------------------------------------------------------


@case("LED-D26", "defect", "other", SV, "statement", "if not user.authenticated:", "return repo.history(",
      "security", "CWE-639", "只校验登录，不校验账户归属（越权）",
      "statement(User('mallory'), repo, 'a1')", "任何登录用户都能看别人的流水")
def _():
    return len(service.statement(User("mallory"), _repo(), "a1")) == 1


@case("LED-D27", "defect", "other", SV, "open_account", "tags: list[str] = []", "tags.append(\"new\")",
      "logic_error", "", "可变默认参数，标签在账户之间共享",
      "连续两次 open_account 不传 tags", "第二个账户的 tags 是 ['new', 'new']，且和第一个是同一个列表")
def _():
    repo = _repo()
    first = service.open_account(repo, "x1", "o")
    second = service.open_account(repo, "x2", "o")
    return first.tags is second.tags and len(second.tags) >= 2


@case("LED-D28", "defect", "other", S, "totals_match", "sum(float(amount) for amount in amounts) == float(expected)", "",
      "logic_error", "CWE-1339", "金额用 float 求和再判等",
      "totals_match(['0.10', '0.20'], '0.30')", "返回 False")
def _():
    return statements.totals_match(["0.10", "0.20"], "0.30") is False


@case("LED-D29", "defect", "other", SV, "refund_card", "log.info(\"refunding %s to card %s\", amount, card)", "",
      "security", "CWE-532", "日志打出完整卡号",
      "任意一次退款", "完整卡号进日志；模块约定必须用 mask()")
def _():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("ledger")
    handler = Grab()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        service.refund_card(type("G", (), {"refund": lambda self, c, a: "rf"})(), "6222020000001234", 1)
    finally:
        logger.removeHandler(handler)
    return any("6222020000001234" in r for r in records)


@case("LED-C14", "clean", "other", S, "statement_total", "Decimal(amount) for amount in amounts", "",
      "logic_error", "", "金额用 Decimal 精确求和")
def _():
    return statements.statement_total(["0.10", "0.20"]) == Decimal("0.30")


# ---- 既有问题（base 里就有，本次没改） -------------------------------------------


@case("LED-P01", "preexisting", "preexisting", MO, "to_cents", "return int(float(text) * 100)", "",
      "logic_error", "CWE-681", "float 乘 100 再截断",
      "to_cents('19.99')", "返回 1998；本次 handle_pay 调用了它，但缺陷不是本次引入，报了算“既有问题”")
def _():
    return to_cents("19.99") == 1998


@case("LED-P02", "preexisting", "preexisting", MO, "format_cents", "return f\"{cents // 100}.{cents % 100:02d}\"", "",
      "logic_error", "CWE-682", "负数格式化错误",
      "format_cents(-50)", "返回 '-1.50'；本次没碰，报了算“既有问题”")
def _():
    return format_cents(-50) == "-1.50"


# ---------------------------------------------------------------------------


def _lines(case: Case, text: str) -> tuple[int, int]:
    lines = text.splitlines()
    name = case.function.split(".")[-1]
    begin = next((i for i, l in enumerate(lines) if l.lstrip().startswith(f"def {name}(")), 0)
    start = next(i for i in range(begin, len(lines)) if case.start in lines[i])
    end = next(i for i in range(start, len(lines)) if case.end in lines[i])
    return start + 1, end + 1


ALSO = {
    "LED-D03": [(R, "Repo.search_memo", "AND {_memo_filter(text)}", "")],
    "LED-D20": [(S, "total_balance", "repo.get_balance(account_id)", "")],
}


def _also(case_id: str) -> list[dict]:
    out = []
    for file, function, start, end in ALSO.get(case_id, []):
        probe = Case(case_id, "", "", file, function, start, end or start, "", "", "", "", "", None)
        lo, hi = _lines(probe, (ROOT / file).read_text())
        out.append({"file": file, "start_line": lo, "end_line": hi})
    return out


def key_rows():
    for c in CASES:
        start, end = _lines(c, (ROOT / c.file).read_text())
        yield {
            "also": _also(c.id),
            "id": c.id, "kind": c.kind, "scenario": c.scenario, "file": c.file,
            "start_line": start, "end_line": end, "function": c.function,
            "category": c.category, "audit_category": LABELS[c.category], "cwe": c.cwe,
            "pairrot_scope": c.category in IN_SCOPE, "title": c.title,
            "trigger": c.trigger, "consequence": c.consequence,
        }


def check_base(sha: str) -> list[str]:
    """Defect anchors that already exist at `sha` (should be none)."""
    found = []
    for c in CASES:
        if c.kind != "defect":
            continue
        try:
            text = subprocess.run(["git", "show", f"{sha}:{c.file}"], cwd=ROOT, capture_output=True,
                                  text=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        try:
            _lines(c, text)
            found.append(c.id)
        except StopIteration:
            pass
    return found


def main(argv: list[str]) -> int:
    failed = 0
    for c in CASES:
        try:
            ok = bool(c.check())
        except Exception as exc:
            ok = False
            print(f"  {c.id} raised {type(exc).__name__}: {exc}")
        failed += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {c.id:8} {c.kind:11} {c.scenario:14} {c.title}")
    kinds = {k: sum(c.kind == k for c in CASES) for k in ("defect", "clean", "preexisting")}
    print(f"\n{len(CASES) - failed}/{len(CASES)} checks hold; {kinds}")
    if "--key" in argv:
        out = OUT / "ledger_answer_key.jsonl"
        with out.open("w", encoding="utf-8") as fh:
            for row in key_rows():
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"wrote {out}")
    if "--base" in argv:
        sha = argv[argv.index("--base") + 1]
        leaked = check_base(sha)
        print(f"defect anchors already at {sha}: {leaked or 'none'}")
        failed += len(leaked)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
