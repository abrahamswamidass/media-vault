"""
`process-intents` — the agent's read side of the intents/ collection.

Verified through the actual CLI entry point (main()), same reasoning as
test_cli_dedup.py: this is the layer where the web-facing contract actually
lives, not just the Action/registry layer underneath it.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from mediavault.cli import main
from mediavault.sync.intents_store import LocalIntentsStore


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _write_intent(intents_dir, *, type_="stage_for_amazon", item_id="Photos/img.jpg",
                  params=None, status="pending", created_at=None, claimed_at=None):
    intent = {
        "id": uuid.uuid4().hex, "type": type_, "item_id": item_id,
        "params": params or {"source": "nas"}, "status": status,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "claimed_at": claimed_at, "result": None,
    }
    (intents_dir / f"{intent['id']}.json").write_text(json.dumps(intent))
    return intent["id"]


def _setup(tmp_path, monkeypatch):
    nas = tmp_path / "nas"
    amazon = tmp_path / "amazon"
    _write(nas, "Photos/img.jpg", b"fake bytes")
    amazon.mkdir()
    db = str(tmp_path / "cat.sqlite")
    intents_dir = tmp_path / "intents"
    intents_dir.mkdir()

    monkeypatch.setenv("NAS_ROOT", str(nas))
    monkeypatch.setenv("AMAZON_STAGING", str(amazon))
    monkeypatch.delenv("NAS_MODE", raising=False)

    assert main(["index", "nas", "--root", str(nas), "--db", db, "--quiet"]) == 0
    return db, intents_dir, amazon


def _common_args(db, intents_dir, tmp_path, *, commit=False, limit=10):
    args = [
        "process-intents", "--db", db, "--intents-dir", str(intents_dir),
        "--blob-dir", str(tmp_path / "blobs"), "--facts-dir", str(tmp_path / "facts"),
        "--log-dir", str(tmp_path / "actions"), "--limit", str(limit),
    ]
    if commit:
        args.append("--commit")
    return args


def test_dry_run_lists_pending_without_claiming_or_running(tmp_path, monkeypatch, capsys):
    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)
    intent_id = _write_intent(intents_dir)

    assert main(_common_args(db, intents_dir, tmp_path, commit=False)) == 0

    out = capsys.readouterr().out
    assert "stage_for_amazon" in out
    assert "DRY-RUN" in out
    assert list(amazon.rglob("*.jpg")) == []
    raw = json.loads((intents_dir / f"{intent_id}.json").read_text())
    assert raw["status"] == "pending"  # untouched — dry-run never claims


def test_commit_claims_runs_and_completes_the_intent(tmp_path, monkeypatch, capsys):
    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)
    intent_id = _write_intent(intents_dir)

    assert main(_common_args(db, intents_dir, tmp_path, commit=True)) == 0

    out = capsys.readouterr().out
    assert "1 done, 0 failed" in out
    staged = list(amazon.rglob("*.jpg"))
    assert len(staged) == 1

    raw = json.loads((intents_dir / f"{intent_id}.json").read_text())
    assert raw["status"] == "done"
    assert raw["result"]["committed"] is True


def test_delete_intent_also_removes_the_items_published_fact(tmp_path, monkeypatch):
    """The web module's "delete" intent (e.g. the Duplicates tab's Archive
    button) has to make the item disappear from Browse/Map/Folders/People
    too, not just move the file on the NAS -- a plain file move with no
    Firestore cleanup would leave it showing everywhere forever."""
    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)
    facts_dir = tmp_path / "facts"
    facts_dir.mkdir()
    (facts_dir / "nas__Photos_img.jpg.json").write_text(
        json.dumps({"source": "nas", "item_id": "Photos/img.jpg"}))
    intent_id = _write_intent(intents_dir, type_="delete", item_id="Photos/img.jpg")

    args = _common_args(db, intents_dir, tmp_path, commit=True)
    assert main(args) == 0

    raw = json.loads((intents_dir / f"{intent_id}.json").read_text())
    assert raw["status"] == "done"
    assert not (facts_dir / "nas__Photos_img.jpg.json").exists()


def test_unknown_intent_type_fails_that_intent_without_crashing_the_batch(tmp_path, monkeypatch):
    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)
    bad_id = _write_intent(intents_dir, type_="not_a_real_type")
    good_id = _write_intent(intents_dir)

    exit_code = main(_common_args(db, intents_dir, tmp_path, commit=True))

    assert exit_code == 1  # at least one failure
    assert json.loads((intents_dir / f"{bad_id}.json").read_text())["status"] == "failed"
    assert json.loads((intents_dir / f"{good_id}.json").read_text())["status"] == "done"


def test_no_pending_intents_is_a_clean_noop(tmp_path, monkeypatch, capsys):
    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)

    assert main(_common_args(db, intents_dir, tmp_path, commit=True)) == 0
    assert "No pending intents." in capsys.readouterr().out


def test_a_stale_claim_is_reclaimed_not_stuck_forever(tmp_path, monkeypatch):
    """An agent that crashed mid-run leaves an intent 'claimed' forever unless
    something notices the lease expired and picks it back up."""
    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)
    stale_claimed_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    intent_id = _write_intent(intents_dir, status="claimed", claimed_at=stale_claimed_at)

    store = LocalIntentsStore(str(intents_dir))
    assert len(store.peek_pending()) == 1

    assert main(_common_args(db, intents_dir, tmp_path, commit=True)) == 0
    assert json.loads((intents_dir / f"{intent_id}.json").read_text())["status"] == "done"


def test_a_fresh_claim_is_not_reclaimed_by_a_concurrent_run(tmp_path, monkeypatch):
    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)
    _write_intent(intents_dir, status="claimed",
                  claimed_at=datetime.now(timezone.utc).isoformat())

    store = LocalIntentsStore(str(intents_dir))
    assert store.peek_pending() == []


# --------------------------------------------------------------------------- #
# FirestoreIntentsStore — regression: the reclaim query used to filter
# "status == claimed AND claimed_at < cutoff" as one query, which Firestore
# rejects without a manually-created composite index (a real crash hit on
# the very first live run — FAILED_PRECONDITION). Fixed to two single-field
# equality queries, filtered client-side; this fake asserts that shape holds.
# --------------------------------------------------------------------------- #
class _FakeDocSnap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeDocRef:
    def __init__(self, doc_id, store):
        self.id = doc_id
        self._store = store

    def update(self, patch):
        self._store[self.id].update(patch)


class _FakeCollection:
    def __init__(self, store, filters=()):
        self._store = store
        self._filters = filters

    def where(self, field, op, value):
        return _FakeCollection(self._store, (*self._filters, (field, op, value)))

    def stream(self):
        assert len(self._filters) <= 1, f"would need a composite index: {self._filters}"
        items = self._store.items()
        if self._filters:
            field, op, value = self._filters[0]
            assert op == "==", f"unsupported op in this fake: {op}"
            items = [(k, v) for k, v in items if v.get(field) == value]
        return iter(_FakeDocSnap(k, v) for k, v in items)

    def document(self, doc_id):
        return _FakeDocRef(doc_id, self._store)


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def collection(self, name):
        return _FakeCollection(self._store)


def test_firestore_reclaim_never_issues_a_query_needing_a_composite_index(monkeypatch):
    from mediavault.sync.intents_store import FirestoreIntentsStore

    store_data = {
        "p1": {"id": "p1", "type": "stage_for_amazon", "status": "pending",
              "created_at": "2020-01-01T00:00:00+00:00"},
        "stale": {"id": "stale", "type": "stage_for_amazon", "status": "claimed",
                 "created_at": "2020-01-01T00:00:00+00:00",
                 "claimed_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
        "fresh": {"id": "fresh", "type": "stage_for_amazon", "status": "claimed",
                 "created_at": "2020-01-01T00:00:00+00:00",
                 "claimed_at": datetime.now(timezone.utc).isoformat()},
        "done": {"id": "done", "type": "stage_for_amazon", "status": "done",
                "created_at": "2020-01-01T00:00:00+00:00"},
    }
    fake_client = _FakeClient(store_data)
    store = FirestoreIntentsStore()
    monkeypatch.setattr(store, "_require_live", lambda: fake_client)

    pending = store.peek_pending()

    assert {r["id"] for r in pending} == {"p1", "stale"}


# --------------------------------------------------------------------------- #
# --watch — the container's own default command, so it has to stop cleanly
# on docker stop's SIGTERM, not just Ctrl+C's SIGINT.
# --------------------------------------------------------------------------- #
def test_stop_on_sigterm_raises_keyboard_interrupt():
    """Direct test of the handler's own logic, not real OS signal delivery —
    os.kill(pid, SIGTERM) doesn't reliably invoke a Python signal handler on
    Windows (it hard-kills the process instead), so this calls the handler
    the same way Python's signal machinery itself would: as a plain
    (signum, frame) callback."""
    import signal

    from mediavault.cli import _stop_on_sigterm

    with pytest.raises(KeyboardInterrupt):
        _stop_on_sigterm(signal.SIGTERM, None)


def test_watch_loop_stops_cleanly_when_sigterm_arrives(tmp_path, monkeypatch, capsys):
    """Full integration of the loop with the handler: simulates SIGTERM
    arriving during the sleep between polls (exactly where `docker stop`
    would catch it) and confirms the loop exits via the same clean path
    Ctrl+C already used, not an unhandled crash."""
    import mediavault.cli as cli_mod

    db, intents_dir, amazon = _setup(tmp_path, monkeypatch)

    def sleep_delivers_sigterm(seconds):
        cli_mod._stop_on_sigterm(0, None)
    monkeypatch.setattr(cli_mod.time, "sleep", sleep_delivers_sigterm)

    args = _common_args(db, intents_dir, tmp_path, commit=True)
    args += ["--watch", "--interval", "600"]

    assert main(args) == 0
    assert "Stopped." in capsys.readouterr().out

