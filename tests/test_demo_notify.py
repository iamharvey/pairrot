"""Happy paths of the notification demo."""

from __future__ import annotations

import json

import pytest

from demo.notify.api import enqueue, recipient_of
from demo.notify.cache import TtlCache
from demo.notify.channels import fit_for
from demo.notify.dispatch import deliver, render_with_retry, schedule, send, send_with_retry
from demo.notify.errors import DeliveryError, GatewayTimeout, TemplateError
from demo.notify.gateway import Gateway
from demo.notify.headers import parse_meta
from demo.notify.models import Notification
from demo.notify.optout import accepts
from demo.notify.prefs import channels_for
from demo.notify.stats import count_by_channel, dedupe_ids, latest_per_user
from demo.notify.storage import write_receipt
from demo.notify.templates import render, template_for
from demo.notify.worker import run


def _n(id="n1", user="u1", channel="email", locale="en", subject="Hi") -> Notification:
    return Notification(id=id, user_id=user, channel=channel, recipient="a@x.io", subject=subject, body="body", locale=locale)


def _gateway(replies):
    calls = []

    def transport(channel, recipient, body):
        calls.append((channel, recipient, body))
        return replies.pop(0) if replies else "m"

    return Gateway(transport), calls


def test_template_for_and_render():
    assert template_for("de") == "Hinweis: {subject}"
    with pytest.raises(TemplateError):
        template_for("xx")
    assert render(_n()) == "Notice: Hi\n\nbody"
    assert render_with_retry(_n(locale="fr")) == "Avis: Hi\n\nbody"


def test_stats():
    items = [_n("1", "u1", "sms"), _n("2", "u2", "email"), _n("3", "u1", "sms")]
    assert count_by_channel(items) == {"sms": 2, "email": 1}
    assert dedupe_ids(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
    assert [n.id for n in latest_per_user(items)] == ["2", "3"]


def test_delivery_retries_timeouts_and_wraps_the_last_one():
    gateway, calls = _gateway([None, "m1"])
    assert send_with_retry(gateway, _n(), "text", sleep=lambda s: None) == "m1"
    assert len(calls) == 2
    gateway, _ = _gateway(["m2"])
    assert deliver(gateway, _n()) == "m2"
    assert send(gateway, _n()) == "m"
    always_down = Gateway(lambda *args: None)
    with pytest.raises(DeliveryError):
        deliver(always_down, _n())
    with pytest.raises(GatewayTimeout):
        send_with_retry(always_down, _n(), "t", attempts=2, sleep=lambda s: None)


def test_schedule_and_worker():
    queue = [_n("1", "u1", "email"), _n("2", "u3", "email"), _n("3", "u3", "sms")]
    assert schedule(queue) == queue
    gateway, _ = _gateway([])
    sent, counts = run(gateway, queue, batch_size=1)
    assert sent == ["m", "m"]
    assert counts == {"email": 1, "sms": 1}


def test_preferences_read_both_record_kinds():
    assert channels_for("u1") == ["email", "sms"]
    assert channels_for("u5") == ["sms", "email"]
    assert channels_for("nobody") == []
    assert accepts("u4", "email") and not accepts("u4", "sms")


def test_api_cache_headers_channels_and_storage(tmp_path):
    queue = []
    n = enqueue(queue, {"user": "u1", "channel": "sms", "recipient": " A@X.io ", "subject": "s", "body": "b"})
    assert n.recipient == "a@x.io" and queue == [n]
    assert recipient_of({"recipient": "r"}) == "r"
    clock = iter([0.0, 1.0, 20.0])
    cache = TtlCache(10, clock=lambda: next(clock))
    loads = []
    assert cache.get("k", lambda key: loads.append(key) or "v") == "v"
    assert cache.get("k", lambda key: "other") == "v"
    assert cache.get("k", lambda key: "fresh") == "fresh"
    assert parse_meta("campaign=spring;;owner='ops;eu';retry=2") == {"campaign": "spring", "owner": "ops;eu", "retry": "2"}
    assert fit_for("sms", "x" * 200) == "x" * 160
    target = write_receipt(str(tmp_path / "spool" / "a"), "n1", "m1")
    assert json.dumps(open(target).read()) == '"m1"'
