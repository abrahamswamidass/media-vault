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
