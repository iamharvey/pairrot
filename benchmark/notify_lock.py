"""Behavior and timing lock for demo/notify. Only public entry points are exercised.

usage: python benchmark/notify_lock.py <tree-root>   prints JSON {"checks": {...}, "perf": {...}}

A simplification is safe when every check that passes at head still passes and no timing
grows by more than 20 percent. The checks cover the decoys on purpose: TTL expiry, ordering
by the newest item, quoted header values, retry on timeouts, and the old preference format.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))

checks: dict[str, str] = {}
perf: dict[str, float] = {}


def check(fn):
    try:
        fn()
        checks[fn.__name__] = "ok"
    except Exception as exc:  # noqa: BLE001 - every failure is a result here
        checks[fn.__name__] = f"fail: {exc.__class__.__name__}: {exc}"[:300]
    return fn


def timed(name, fn, repeat=3):
    try:
        best = min(_once(fn) for _ in range(repeat))
        perf[name] = round(best, 5)
    except Exception as exc:  # noqa: BLE001
        perf[name] = -1.0
        checks[f"perf_{name}"] = f"fail: {exc.__class__.__name__}: {exc}"[:300]


def _once(fn):
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


try:
    from demo.notify import api, cache, channels, dispatch, headers, optout, prefs, stats, storage, templates, worker
    from demo.notify.errors import DeliveryError, GatewayTimeout, TemplateError
    from demo.notify.gateway import Gateway
    from demo.notify.models import Notification
except Exception:  # noqa: BLE001
    print(json.dumps({"checks": {"import": "fail: " + traceback.format_exc(limit=1)[-300:]}, "perf": {}}))
    raise SystemExit(0)


def n(id="n1", user="u1", channel="email", locale="en", subject="Hi", body="body"):
    return Notification(id=id, user_id=user, channel=channel, recipient="a@x.io", subject=subject, body=body, locale=locale)


def gateway(replies=None):
    replies = list(replies or [])
    calls = []

    def transport(channel, recipient, body):
        calls.append((channel, recipient, body))
        return replies.pop(0) if replies else f"m{len(calls)}"

    return Gateway(transport), calls


@check
def count_by_channel():
    items = [n("1", channel="sms"), n("2", channel="email"), n("3", channel="sms")]
    got = stats.count_by_channel(items)
    assert got == {"sms": 2, "email": 1} and list(got) == ["sms", "email"]
    assert stats.count_by_channel([]) == {}


@check
def dedupe_ids():
    assert stats.dedupe_ids(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
    assert stats.dedupe_ids([]) == []


@check
def latest_per_user():
    items = [n("1", "u1"), n("2", "u2"), n("3", "u1"), n("4", "u3")]
    assert [x.id for x in stats.latest_per_user(items)] == ["2", "3", "4"]


@check
def templates_and_render():
    assert templates.template_for("de") == "Hinweis: {subject}"
    for _ in range(2):
        try:
            templates.template_for("xx")
        except TemplateError:
            pass
        else:
            raise AssertionError("unknown locale must raise every time")
    assert templates.format_subject(n(), "fr") == "Avis: Hi"
    assert templates.render(n()) == "Notice: Hi\n\nbody"


@check
def deliver_paths():
    gw, calls = gateway()
    assert dispatch.deliver(gw, n(channel="sms", body="x" * 300)) == "m1"
    assert len(calls[0][2]) == 160
    try:
        dispatch.deliver(gw, n(locale="xx"))
    except TemplateError:
        pass
    else:
        raise AssertionError("TemplateError must propagate")
    gw, calls = gateway([None, None, "late"])
    assert dispatch.deliver(gw, n()) == "late" and len(calls) == 3
    down = Gateway(lambda *args: None)
    try:
        dispatch.deliver(down, n("n9"))
    except DeliveryError as exc:
        assert exc.notification_id == "n9" and isinstance(exc.__cause__, GatewayTimeout)
    else:
        raise AssertionError("timeouts must become DeliveryError")


@check
def worker_runs():
    queue = [n("1", "u1", "email"), n("2", "u3", "email"), n("3", "u3", "sms"), n("4", "u4", "email")]
    gw, calls = gateway()
    sent, counts = worker.run(gw, queue, batch_size=1)
    assert sent == ["m1", "m2", "m3"] and counts == {"email": 2, "sms": 1}
    gw, _ = gateway()
    assert worker.run_nightly(gw, queue)[1] == {"email": 2, "sms": 1}


@check
def preferences():
    assert prefs.channels_for("u1") == ["email", "sms"]
    assert prefs.channels_for("u5") == ["sms", "email"]
    assert prefs.channels_for("nobody") == []
    assert optout.accepts("u2", "email") and not optout.accepts("u2", "sms")


@check
def api_enqueue():
    queue = []
    got = api.enqueue(queue, {"user": "u1", "channel": "sms", "recipient": " A@X.io ", "subject": "s", "body": "b"})
    assert got.recipient == "a@x.io" and got.locale == "en" and queue == [got]
    try:
        api.enqueue([], {"user": "u1", "channel": "sms", "subject": "s", "body": "b"})
    except KeyError:
        pass
    else:
        raise AssertionError("missing recipient must raise KeyError")


@check
def ttl_cache():
    now = [0.0]
    store = cache.TtlCache(10, clock=lambda: now[0])
    assert store.get("k", lambda key: "v1") == "v1"
    now[0] = 5
    assert store.get("k", lambda key: "v2") == "v1"
    now[0] = 11
    assert store.get("k", lambda key: "v3") == "v3"


@check
def meta_header():
    assert headers.parse_meta("campaign=spring;;owner='ops;eu';retry=2") == {"campaign": "spring", "owner": "ops;eu", "retry": "2"}
    assert headers.parse_meta("flag;a=1") == {"a": "1"}
    assert headers.parse_meta("") == {}


@check
def channels_fit():
    assert channels.fit_for("email", "x" * 300) == "x" * 300
    assert channels.fit_for("sms", "x" * 300) == "x" * 160
    assert channels.fit_for("push", "abc") == "abc"


@check
def receipts():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        spool = str(Path(tmp) / "a" / "b")
        first = storage.write_receipt(spool, "n1", "m1")
        storage.write_receipt(spool, "n2", "m2")
        assert Path(first).read_text() == "m1"


big = [n(str(i), f"u{i % 5000}", ("email", "sms")[i % 2]) for i in range(200_000)]
ids = [str(i % 3000) for i in range(6000)]
lines = ["campaign=spring;;owner='ops;eu';retry=2"] * 20_000
timed("count_by_channel", lambda: stats.count_by_channel(big))
timed("dedupe_ids", lambda: stats.dedupe_ids(ids))
timed("latest_per_user", lambda: stats.latest_per_user(big))
timed("parse_meta", lambda: [headers.parse_meta(line) for line in lines])
timed("template_for", lambda: [templates.template_for("de") for _ in range(200_000)])
timed("render", lambda: [templates.render(x) for x in big[:50_000]])
store = cache.TtlCache(60)
timed("ttl_cache", lambda: [store.get(i % 100, str) for i in range(200_000)])
timed("worker", lambda: worker.run(gateway()[0], big[:3000], batch_size=100))

print(json.dumps({"checks": checks, "perf": perf}, ensure_ascii=False))
