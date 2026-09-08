"""
Wiring test for _maybe_notify_fullres_ready: a completed `fetch_fullres`
intent should trigger a Web Push notification when NOTIFY_LIVE=1, and never
otherwise. notify.send() and FirestorePushSubscriptions are faked here —
notify.py's own protocol-level logic is covered by test_notify.py.
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone

import pytest

PIL = pytest.importorskip("PIL", reason="fetch_fullres needs Pillow (imaging extra)")
from PIL import Image  # noqa: E402

from mediavault.cli import main
from mediavault import notify as notify_mod
from mediavault.sync.push_subscriptions import FirestorePushSubscriptions


def _jpeg_bytes(size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(120, 180, 90)).save(buf, format="JPEG")
    return buf.getvalue()


def _write_intent(intents_dir, *, item_id="Photos/img.jpg"):
    intent = {
        "id": uuid.uuid4().hex, "type": "fetch_fullres", "item_id": item_id,
        "params": {"source": "nas", "variant": "preview"}, "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claimed_at": None, "result": None,
    }
    (intents_dir / f"{intent['id']}.json").write_text(json.dumps(intent))
    return intent["id"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    (nas / "Photos").mkdir(parents=True)
    (nas / "Photos" / "img.jpg").write_bytes(_jpeg_bytes())
    db = str(tmp_path / "cat.sqlite")
    intents_dir = tmp_path / "intents"
    intents_dir.mkdir()

    monkeypatch.setenv("NAS_ROOT", str(nas))
    monkeypatch.delenv("NAS_MODE", raising=False)
    assert main(["index", "nas", "--root", str(nas), "--db", db, "--quiet"]) == 0

    args = [
        "process-intents", "--db", db, "--intents-dir", str(intents_dir),
        "--blob-dir", str(tmp_path / "blobs"), "--facts-dir", str(tmp_path / "facts"),
        "--log-dir", str(tmp_path / "actions"), "--limit", "10", "--commit",
    ]
    return args, intents_dir


@pytest.fixture
def fake_subscriptions(monkeypatch):
    """One registered device, and a place to record what was sent/removed."""
    sent = []
    removed = []
    monkeypatch.setattr(
        FirestorePushSubscriptions, "list_all",
        lambda self: [{"id": "dev1", "endpoint": "https://push.example/1",
                       "keys": {"p256dh": "p", "auth": "a"}}])
    monkeypatch.setattr(
        FirestorePushSubscriptions, "remove", lambda self, doc_id: removed.append(doc_id))

    def fake_send(subscription, *, title, body, url, vapid_private_key, vapid_subject):
        sent.append({"subscription": subscription, "title": title, "body": body, "url": url})
        return True
    monkeypatch.setattr(notify_mod, "send", fake_send)
    return sent, removed


def test_notify_live_off_by_default_sends_nothing(setup, fake_subscriptions):
    args, intents_dir = setup
    _write_intent(intents_dir)
    sent, removed = fake_subscriptions

    assert main(args) == 0

    assert sent == []


def test_notify_live_sends_to_every_registered_device(setup, fake_subscriptions, monkeypatch):
    args, intents_dir = setup
    _write_intent(intents_dir)
    sent, removed = fake_subscriptions
    monkeypatch.setenv("NOTIFY_LIVE", "1")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "fake-private-key")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:me@example.com")

    assert main(args) == 0

    assert len(sent) == 1
    assert sent[0]["title"] == "Full-res ready"
    assert sent[0]["body"] == "img.jpg"  # just the filename, not the full path
    assert removed == []


def test_notify_live_without_vapid_keys_skips_quietly(setup, fake_subscriptions, monkeypatch, capsys):
    args, intents_dir = setup
    _write_intent(intents_dir)
    sent, removed = fake_subscriptions
    monkeypatch.setenv("NOTIFY_LIVE", "1")
    # No VAPID_PRIVATE_KEY / VAPID_SUBJECT set.

    assert main(args) == 0  # the fetch_fullres intent itself still succeeds

    assert sent == []
    assert "skipping notify" in capsys.readouterr().out


def test_a_stale_subscription_is_pruned(setup, fake_subscriptions, monkeypatch):
    args, intents_dir = setup
    _write_intent(intents_dir)
    sent, removed = fake_subscriptions
    monkeypatch.setenv("NOTIFY_LIVE", "1")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "fake-private-key")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:me@example.com")
    monkeypatch.setattr(notify_mod, "send", lambda *a, **kw: False)  # 404/410, gone

    assert main(args) == 0

    assert removed == ["dev1"]
