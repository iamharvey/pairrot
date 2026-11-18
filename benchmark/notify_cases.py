"""Answer key for demo/notify: unnecessary complexity, intentional incompatibility, and decoys.

usage (from the repo root):
  python benchmark/notify_cases.py            check anchors and the two executable defects
  python benchmark/notify_cases.py --key      also write benchmark/notify_answer_key.jsonl

Line numbers come from anchors in the head files, so editing the demo does not stale the key.
locked says whether a test in tests/ mentions the enclosing function, which decides the pairrot level.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("NOTIFY_ROOT") or Path(__file__).resolve().parents[1])
OUT = Path(__file__).resolve().parent


@dataclass
class Case:
    id: str
    kind: str  # defect or decoy
    scenario: str
    file: str
    function: str
    start: str
    end: str
    categories: list[str]
    locked: bool
    title: str
    why: str
    also: list[tuple[str, str]] = field(default_factory=list)


C = "complexity"
CASES = [
    Case("NTF-D01", "defect", "stdlib", "stats.py", "count_by_channel", "counts: dict[str, int] = {}", "return counts", [C], True,
         "手写字典计数", "dict(Counter(n.channel for n in items)) 等价，插入顺序相同"),
    Case("NTF-D02", "defect", "stdlib", "stats.py", "dedupe_ids", 'seen: list[str] = []', "return seen", [C, "performance"], True,
         "列表去重是平方复杂度", "list(dict.fromkeys(ids)) 等价且是线性；批次最多 20 万"),
    Case("NTF-D03", "defect", "stdlib", "templates.py", "template_for", "if locale in _template_cache:", "return value", [C], True,
         "手写 memo 缓存纯函数", "模板在进程内不变，functools.cache 等价，异常同样不缓存"),
    Case("NTF-D04", "defect", "ceremony", "dispatch.py", "render_with_retry", "last_error = None", "raise last_error", [C], True,
         "给纯函数 render 加重试", "render 是纯本地计算，重试只会得到同样的异常"),
    Case("NTF-D05", "defect", "ceremony", "dispatch.py", "deliver", "except TemplateError as exc:", "raise exc", [C, "error_handling"], True,
         "捕获后原样重抛", "去掉这层 try 行为不变"),
    Case("NTF-D06", "defect", "delete", "dispatch.py", "send", "def send(gateway: Gateway, n: Notification) -> str:", "return deliver(gateway, n)", [C], True,
         "只转发的包装层", "唯一调用方 SequentialStrategy 可以直接调 deliver"),
    Case("NTF-D07", "defect", "compat_shim", "dispatch.py", "send_email", "# Older code called this send_email.", "send_email = deliver", [C], True,
         "没人用的旧名字别名", "全仓没有 send_email 的使用者"),
    Case("NTF-D08", "defect", "speculative", "dispatch.py", "schedule", "def schedule(batch: list[Notification], *, order", "raise ValueError(order)", [C], True,
         "没人传的 order 和 jitter 参数", "唯一调用方不传 order 和 jitter，jitter 也没被使用"),
    Case("NTF-D09", "defect", "delete", "dispatch.py", "DispatchStrategy", "class DispatchStrategy(ABC):", 'raise ValueError(f"unknown strategy {name}")', [C], False,
         "只有一个实现的策略和工厂", "唯一调用点固定传 sequential", also=[("worker.py", 'StrategyFactory.create("sequential")')]),
    Case("NTF-D10", "defect", "dead_branch", "templates.py", "render", "if USE_V2_RENDERER:", 'return f"{n.subject}\\n{n.body}"', [C, "logic_error"], True,
         "常量开关后面的死分支", "USE_V2_RENDERER 恒为 True，else 永远不走", also=[("templates.py", "USE_V2_RENDERER = True")]),
    Case("NTF-D11", "defect", "compat_shim", "api.py", "recipient_of", 'value = payload.get("recipient")', 'raise KeyError("recipient")', [C], True,
         "给不存在的旧字段 recipient_addr 留兜底", "模块文档写明客户端只发 recipient，全仓没有写 recipient_addr 的地方"),
    Case("NTF-D12", "defect", "reinvent", "api.py", "_normalize_email", "def _normalize_email(text: str) -> str:", "return text.strip().lower()", [C], False,
         "重复实现 util.normalize_address", "util.normalize_address 已有同样实现"),
    Case("NTF-D13", "defect", "stdlib", "storage.py", "ensure_dir", "if not os.path.exists(path):", "os.makedirs(path)", [C, "concurrency"], False,
         "先判断再创建目录", "os.makedirs(path, exist_ok=True)；路径是普通文件时两者都会在后面失败，只是报错位置不同"),
    Case("NTF-D14", "defect", "contract", "templates.py", "format_subject", "def format_subject(n: Notification, locale: str) -> str:", "def format_subject(n: Notification, locale: str) -> str:",
         ["contract_break", "logic_error"], True,
         "format_subject 加了必填参数，digest.py 还按旧签名调用", "digest_lines 会抛 TypeError", also=[("digest.py", "format_subject(n)")]),
    Case("NTF-X01", "decoy", "intentional_break", "prefs.py", "channels_for", "def channels_for(user_id: str, path: Path = DATA) -> list[str]:", "return []", ["contract_break"], True,
         "返回值从字符串改成列表", "故意的不兼容，仓库内唯一调用方 optout.accepts 已同步修改", also=[("optout.py", "return channel in channels_for(user_id)")]),
    Case("NTF-X02", "decoy", "needed_compat", "prefs.py", "channels_for", 'return [item for item in record["prefs_v1"].split(",") if item]', 'return [item for item in record["prefs_v1"].split(",") if item]', [C], True,
         "读取旧格式 prefs_v1", "数据文件里仍有旧格式记录，去掉会读不出 u1-u3"),
    Case("NTF-X03", "decoy", "two_impls", "channels.py", "Channel", "class Channel(Protocol):", "return found.fit(text) if found else text", [C], True,
         "Channel 协议", "有 Email 和 Sms 两个实现，行为不同"),
    Case("NTF-X04", "decoy", "needed_retry", "dispatch.py", "send_with_retry", "attempt = 0", "sleep(backoff * 2 ** (attempt - 1))", [C], True,
         "网络调用的重试", "网关是远程服务，会超时；不能引入 tenacity，项目没有这个依赖"),
    Case("NTF-X05", "decoy", "not_equivalent", "cache.py", "TtlCache", "now = self.clock()", "return value", [C], True,
         "带过期时间的缓存", "lru_cache 没有过期，换掉后会读到过时的偏好"),
    Case("NTF-X06", "decoy", "not_equivalent", "stats.py", "latest_per_user", "latest: dict[str, Notification] = {}", "return list(latest.values())", [C], True,
         "先 pop 再插入", "字典推导会保留用户第一次出现的位置，结果顺序不同"),
    Case("NTF-X07", "decoy", "not_equivalent", "headers.py", "parse_meta", "fields: list[str] = []", "return result", [C], True,
         "手写的头部解析", "split、parse_qsl、csv 都处理不了单引号里的分号"),
    Case("NTF-X08", "decoy", "extension_point", "plugins/renderers.py", "Renderer", "class Renderer(ABC):", 'return f"{n.subject}\\n{n.body}"', [C], False,
         "扩展点上只有一个实现的基类", "REVIEW.toml 把 plugins 声明为扩展点"),
    Case("NTF-X09", "decoy", "used_param", "worker.py", "run", "def run(gateway: Gateway, queue: list[Notification], batch_size: int = 50)", "def run(gateway: Gateway, queue: list[Notification], batch_size: int = 50)", [C], True,
         "batch_size 参数", "run_nightly 传 500"),
    Case("NTF-X10", "decoy", "adds_context", "dispatch.py", "deliver", "except GatewayTimeout as exc:", 'raise DeliveryError(n.id, "gateway timeout") from exc', [C, "error_handling"], True,
         "超时转成 DeliveryError", "带上了通知 id，不是原样重抛"),
]


def _line(file: str, anchor: str, after: int = 0) -> int:
    rows = (ROOT / "demo" / "notify" / file).read_text(encoding="utf-8").splitlines()
    for number, row in enumerate(rows, start=1):
        if number > after and anchor in row:
            return number
    raise AssertionError(f"anchor not found in {file}: {anchor}")


def key_rows() -> list[dict]:
    rows = []
    for case in CASES:
        start = _line(case.file, case.start)
        end = _line(case.file, case.end, after=start - 1)
        row = asdict(case)
        row.pop("start"), row.pop("end"), row.pop("also")
        row.update(
            file=f"demo/notify/{case.file}",
            start_line=start,
            end_line=end,
            also=[
                {"file": f"demo/notify/{path}", "start_line": _line(path, text), "end_line": _line(path, text)}
                for path, text in case.also
            ],
        )
        rows.append(row)
    return rows


def check_defects() -> None:
    sys.path.insert(0, str(ROOT))
    from demo.notify.digest import digest_lines
    from demo.notify.models import Notification
    from demo.notify.stats import dedupe_ids

    try:
        digest_lines([Notification("n", "u", "email", "a@x", "s", "b")])
    except TypeError:
        pass
    else:
        raise AssertionError("NTF-D14: digest_lines should raise TypeError at head")
    small, large = [str(i) for i in range(1000)], [str(i) for i in range(4000)]
    started = time.perf_counter()
    dedupe_ids(small)
    first = time.perf_counter() - started
    started = time.perf_counter()
    dedupe_ids(large)
    second = time.perf_counter() - started
    assert second > first * 8, "NTF-D02: dedupe_ids should grow faster than linearly"


if __name__ == "__main__":
    rows = key_rows()
    check_defects()
    print(f"{len(rows)} rows, {sum(r['kind'] == 'defect' for r in rows)} defects, {sum(r['kind'] == 'decoy' for r in rows)} decoys")
    if "--key" in sys.argv:
        with open(OUT / "notify_answer_key.jsonl", "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print("wrote", OUT / "notify_answer_key.jsonl")
