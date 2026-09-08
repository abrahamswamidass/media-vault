"""
Web Push — the one place that sends a browser notification.

Mirrors metadata.py's shape: pywebpush is an optional extra, imported only on
use, so the core harness stays runnable without it. Gated behind NOTIFY_LIVE=1
in cli.py, same as every other live switch in this project.

A stale subscription (the browser unsubscribed, or the push service expired
the endpoint) is routine, not a failure — the push service reports it as a
404/410, and the caller (cli.py) prunes that subscription from Firestore
rather than treating it as an error worth surfacing.
"""
from __future__ import annotations

import json
from typing import Optional


class NotifyUnavailable(RuntimeError):
    """pywebpush isn't installed."""


def send(subscription: dict, *, title: str, body: str, url: str,
         vapid_private_key: str, vapid_subject: str) -> bool:
    """Push one notification to one subscription.

    `subscription` is the raw {"endpoint", "keys": {"p256dh", "auth"}} object
    the browser produced from PushManager.subscribe() (see web/push.js).

    Returns True on success, False if the subscription is gone (caller should
    delete it) — never raises for that case, since an expired subscription on
    one of possibly several devices shouldn't crash the intents loop over
    the rest.
    """
    try:
        from pywebpush import WebPushException, webpush  # noqa: PLC0415 — optional dependency
    except ImportError as e:
        raise NotifyUnavailable(
            "pywebpush is required to send notifications. "
            "pip install pywebpush (already listed in requirements.txt)."
        ) from e

    payload = json.dumps({"title": title, "body": body, "url": url})
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid_private_key,
            vapid_claims={"sub": vapid_subject},
        )
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            return False
        raise
