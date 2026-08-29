"""
Intents adapters — where the web module's requests actually live.

Peer of `facts.py`: that file is the agent's only write path to the web module,
this is the agent's only read path from it. `LocalIntentsStore` makes the
processor runnable and testable with no cloud account, exactly like
`LocalFactsStore` does for publish.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..ports import IntentsStore
from .intents import CLAIM_LEASE_SECONDS, CLAIMED, DONE, FAILED, PENDING


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale(raw: dict, cutoff: datetime) -> bool:
    """True if a 'claimed' intent's claim is old enough to assume the agent
    that claimed it crashed mid-run, rather than still being in flight."""
    claimed_at = raw.get("claimed_at")
    if not claimed_at:
        return True
    return datetime.fromisoformat(claimed_at) < cutoff


def _claimable(raw: dict) -> bool:
    """Pending outright, or a stale claim (see `_is_stale`)."""
    if raw.get("status") == PENDING:
        return True
    if raw.get("status") != CLAIMED:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_LEASE_SECONDS)
    return _is_stale(raw, cutoff)


class LocalIntentsStore(IntentsStore):
    """One JSON file per intent, named by id. Used by tests and local runs."""
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, intent_id: str) -> Path:
        return self.root / f"{intent_id}.json"

    def _all(self) -> list[dict]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def _pending_sorted(self, limit: int) -> list[dict]:
        rows = [r for r in self._all() if _claimable(r)]
        rows.sort(key=lambda r: r.get("created_at", ""))
        return rows[:limit]

    def peek_pending(self, limit: int = 10) -> list[dict]:
        return self._pending_sorted(limit)

    def claim_pending(self, limit: int = 10) -> list[dict]:
        claimed = []
        for raw in self._pending_sorted(limit):
            raw["status"] = CLAIMED
            raw["claimed_at"] = _now_iso()
            self._path(raw["id"]).write_text(json.dumps(raw, indent=2, default=str))
            claimed.append(raw)
        return claimed

    def _set_terminal(self, intent_id: str, status: str, result: dict) -> None:
        p = self._path(intent_id)
        raw = json.loads(p.read_text()) if p.exists() else {"id": intent_id}
        raw["status"] = status
        raw["result"] = result
        p.write_text(json.dumps(raw, indent=2, default=str))

    def complete(self, intent_id: str, result: dict) -> None:
        self._set_terminal(intent_id, DONE, result)

    def fail(self, intent_id: str, result: dict) -> None:
        self._set_terminal(intent_id, FAILED, result)


class FirestoreIntentsStore(IntentsStore):
    """The real target. Guarded behind GCS_LIVE like every other live client
    in this project — same env var FirestoreFactsStore/GCSBlobStore use, since
    Firestore + Cloud Storage share one service-account credential.

    No transactional claim: this project runs exactly one agent (see
    CLAUDE.md — "a **local agent**", singular), so there is no second writer
    to race against. A crashed agent's abandoned claim is picked back up by
    `_claimable`'s lease check, not by transaction isolation.
    """
    name = "firestore"

    def __init__(self, collection: str = "intents", database: str | None = None):
        self.collection = collection
        self.database = database or os.getenv("FIRESTORE_DATABASE") or "(default)"
        self.live = os.getenv("GCS_LIVE", "0") == "1"
        self._client = None

    def _require_live(self):
        if not self.live:
            raise NotImplementedError(
                "Firestore is in SAFE mode (GCS_LIVE!=1). Use LocalIntentsStore, "
                "or set GCS_LIVE=1 once credentials are in place."
            )
        if self._client is None:
            from google.cloud import firestore  # optional extra — only imported when GCS_LIVE=1

            self._client = firestore.Client(database=self.database)
        return self._client

    def _pending_sorted(self, limit: int) -> list[dict]:
        client = self._require_live()
        coll = client.collection(self.collection)
        # Two single-field equality queries, merged and lease-filtered
        # client-side — filtering "claimed AND claimed_at < cutoff" as one
        # query needs a composite index (two different fields), which this
        # project deliberately avoids setting up (no manual Firebase console
        # step beyond pasting firestore.rules). A single equality filter
        # never needs one, and the claimed set is small — only intents
        # actively in flight, never the whole library.
        pending = list(coll.where("status", "==", PENDING).stream())
        claimed = list(coll.where("status", "==", CLAIMED).stream())
        stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_LEASE_SECONDS)
        stale = [d for d in claimed if _is_stale(d.to_dict(), stale_cutoff)]
        rows = [{**d.to_dict(), "id": d.id} for d in (*pending, *stale)]
        rows.sort(key=lambda r: r.get("created_at", ""))
        return rows[:limit]

    def peek_pending(self, limit: int = 10) -> list[dict]:
        return self._pending_sorted(limit)

    def claim_pending(self, limit: int = 10) -> list[dict]:
        client = self._require_live()
        claimed = []
        for raw in self._pending_sorted(limit):
            claimed_at = _now_iso()
            client.collection(self.collection).document(raw["id"]).update(
                {"status": CLAIMED, "claimed_at": claimed_at})
            raw["status"] = CLAIMED
            raw["claimed_at"] = claimed_at
            claimed.append(raw)
        return claimed

    def complete(self, intent_id: str, result: dict) -> None:
        client = self._require_live()
        client.collection(self.collection).document(intent_id).update(
            {"status": DONE, "result": result})

    def fail(self, intent_id: str, result: dict) -> None:
        client = self._require_live()
        client.collection(self.collection).document(intent_id).update(
            {"status": FAILED, "result": result})

    def heartbeat(self, pending_count: int) -> None:
        # Fixed document id, not a growing collection: the web UI only ever
        # needs the latest heartbeat, never a history of them.
        client = self._require_live()
        client.collection("agent_status").document("process_intents").set({
            "last_poll_at": _now_iso(),
            "pending_count": pending_count,
        })
