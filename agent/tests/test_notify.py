"""
notify.py — Web Push sending. The real `pywebpush` package isn't installed in
this dev sandbox (or necessarily in CI), so these inject a fake module, same
approach test_metadata.py uses for PyExifTool: only notify.send()'s own logic
(payload shape, stale-subscription handling) is under test here, not the real
pywebpush/Web Push protocol integration.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from mediavault import notify


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeWebPushException(Exception):
    def __init__(self, message="", response=None):
        super().__init__(message)
        self.response = response


@pytest.fixture
def fake_pywebpush(monkeypatch):
    """Injects a fake `pywebpush` module whose webpush() either succeeds or
    raises WebPushException, controlled per-test via state["raise_status"]."""
    state = {"raise_status": None, "calls": []}

    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims):
        state["calls"].append({
            "subscription_info": subscription_info, "data": data,
            "vapid_private_key": vapid_private_key, "vapid_claims": vapid_claims,
        })
        if state["raise_status"] is not None:
            raise _FakeWebPushException(response=_FakeResponse(state["raise_status"]))

    fake_module = types.SimpleNamespace(
        webpush=fake_webpush, WebPushException=_FakeWebPushException)
    monkeypatch.setitem(sys.modules, "pywebpush", fake_module)
    return state


_SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "p", "auth": "a"}}


def test_send_success_returns_true_and_encodes_payload(fake_pywebpush):
    ok = notify.send(
        _SUB, title="Full-res ready", body="IMG_0001.jpg", url="/",
        vapid_private_key="fake-key", vapid_subject="mailto:me@example.com",
    )

    assert ok is True
    call = fake_pywebpush["calls"][0]
    assert call["subscription_info"] == _SUB
    assert json.loads(call["data"]) == {
        "title": "Full-res ready", "body": "IMG_0001.jpg", "url": "/"}
    assert call["vapid_claims"] == {"sub": "mailto:me@example.com"}


@pytest.mark.parametrize("status", [404, 410])
def test_stale_subscription_returns_false_not_raise(fake_pywebpush, status):
    fake_pywebpush["raise_status"] = status

    ok = notify.send(
        _SUB, title="t", body="b", url="/",
        vapid_private_key="fake-key", vapid_subject="mailto:me@example.com",
    )

    assert ok is False


def test_other_push_failure_propagates(fake_pywebpush):
    fake_pywebpush["raise_status"] = 500

    with pytest.raises(_FakeWebPushException):
        notify.send(
            _SUB, title="t", body="b", url="/",
            vapid_private_key="fake-key", vapid_subject="mailto:me@example.com",
        )


def test_missing_pywebpush_raises_notify_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "pywebpush", None)  # forces ImportError on import

    with pytest.raises(notify.NotifyUnavailable):
        notify.send(
            _SUB, title="t", body="b", url="/",
            vapid_private_key="fake-key", vapid_subject="mailto:me@example.com",
        )
