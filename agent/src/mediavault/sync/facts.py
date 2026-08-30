"""
Facts adapters — where cataloged item metadata goes for the web module to read.

Peer of `blobstore.py`: that file holds the derived *bytes* (thumbnails,
previews), this one holds the *metadata* pointing at them (name, size, dates,
which blob key to fetch). Same Ports & Adapters split, same reason it lives in
`sync/` rather than `catalog/` — this is the agent's half of "intents in, facts
out": the web module only ever reads what lands here, never writes it.

`LocalFactsStore` makes the publish pipeline runnable and testable with no
cloud account, exactly like `LocalBlobStore` does for thumbnails.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..ports import FactsStore

#: Firestore document IDs can't contain '/'. item_id is a path, so flatten it.
_SLASH_RE = re.compile(r"[/\\]+")


def doc_id(source: str, item_id: str) -> str:
    """Deterministic Firestore-safe document id for one catalog item."""
    return f"{source}__{_SLASH_RE.sub('_', item_id)}"


class LocalFactsStore(FactsStore):
    """Writes one JSON file per item. Used by tests and `--offline` runs."""
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, source: str, item_id: str, fact: dict) -> None:
        p = self.root / f"{doc_id(source, item_id)}.json"
        p.write_text(json.dumps(fact, indent=2, default=str))

    def delete(self, source: str, item_id: str) -> None:
        p = self.root / f"{doc_id(source, item_id)}.json"
        p.unlink(missing_ok=True)

    def purge(self, source: str | None = None) -> int:
        prefix = f"{source}__" if source else ""
        deleted = 0
        for p in self.root.glob("*.json"):
            if not source or p.name.startswith(prefix):
                p.unlink()
                deleted += 1
        return deleted


class FirestoreFactsStore(FactsStore):
    """The real target. Ships guarded, same pattern as GCSBlobStore/DriveConnector:
    the shape is real, network calls stay behind an explicit env check so a
    half-configured machine can't surprise you with a write.

    Reuses GCS_LIVE — Firestore and Cloud Storage are both part of the one
    "GCP mirror push" per requirements.txt, and share the same service-account
    credentials via GOOGLE_APPLICATION_CREDENTIALS.

    Writes to the `items` collection — per CLAUDE.md's write-ownership rule,
    `items/` is agent-written, `intents/` is web-written.
    """
    name = "firestore"

    def __init__(self, collection: str = "items", database: str | None = None):
        self.collection = collection
        # A GCP project can hold multiple named Firestore databases; "(default)"
        # only exists if one was created under that exact name. Anything else —
        # e.g. a database named "media-vault-store" — must be passed explicitly,
        # or the client looks for "(default)" and gets NOT_FOUND.
        self.database = database or os.getenv("FIRESTORE_DATABASE") or "(default)"
        self.live = os.getenv("GCS_LIVE", "0") == "1"
        self._client = None

    def _require_live(self):
        if not self.live:
            raise NotImplementedError(
                "Firestore is in SAFE mode (GCS_LIVE!=1). Use LocalFactsStore, "
                "or set GCS_LIVE=1 once credentials are in place."
            )
        if self._client is None:
            from google.cloud import firestore  # optional extra — only imported when GCS_LIVE=1

            self._client = firestore.Client(database=self.database)
        return self._client

    def put(self, source: str, item_id: str, fact: dict) -> None:
        client = self._require_live()
        client.collection(self.collection).document(doc_id(source, item_id)).set(fact)

    def delete(self, source: str, item_id: str) -> None:
        # Deleting a Firestore document that doesn't exist is already a
        # no-op server-side, not an error -- nothing extra needed to make
        # this safe to retry.
        client = self._require_live()
        client.collection(self.collection).document(doc_id(source, item_id)).delete()

    def purge(self, source: str | None = None) -> int:
        client = self._require_live()
        coll = client.collection(self.collection)
        docs = coll.where("source", "==", source).stream() if source else coll.stream()
        deleted = 0
        for doc in docs:
            doc.reference.delete()
            deleted += 1
        return deleted
