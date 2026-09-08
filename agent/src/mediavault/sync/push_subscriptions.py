"""
Push subscriptions — where a browser's PushManager.subscribe() result lands.

Peer of facts.py/intents_store.py, but simpler and one-directional: the web
module is the only writer (see web/push.js — a client-owned collection, like
hidden_folders/, not an intent; subscribing is a device preference, not a
file mutation). The agent only ever reads this collection to know who to
notify, and prunes a subscription the push service reports as gone (see
notify.py's 404/410 handling).

No LocalPushSubscriptionsStore: unlike facts/intents, there's no meaningful
offline stand-in for "a real browser's push endpoint" worth building parity
for — this store is only ever exercised behind NOTIFY_LIVE, itself only
meaningful once GCS_LIVE=1 (same service-account credentials as Firestore
everywhere else in this project).
"""
from __future__ import annotations

import os


class FirestorePushSubscriptions:
    """Reads/prunes the `push_subscriptions` collection. Guarded behind
    GCS_LIVE, same as every other Firestore-backed adapter here."""
    name = "firestore"

    def __init__(self, collection: str = "push_subscriptions", database: str | None = None):
        self.collection = collection
        self.database = database or os.getenv("FIRESTORE_DATABASE") or "(default)"
        self.live = os.getenv("GCS_LIVE", "0") == "1"
        self._client = None

    def _require_live(self):
        if not self.live:
            raise NotImplementedError(
                "Firestore is in SAFE mode (GCS_LIVE!=1). Set GCS_LIVE=1 once "
                "credentials are in place."
            )
        if self._client is None:
            from google.cloud import firestore  # noqa: PLC0415 — optional extra, only imported when GCS_LIVE=1

            self._client = firestore.Client(database=self.database)
        return self._client

    def list_all(self) -> list[dict]:
        """Every registered device, each as {"id", "endpoint", "keys": {...}}."""
        client = self._require_live()
        return [{**d.to_dict(), "id": d.id}
                for d in client.collection(self.collection).stream()]

    def remove(self, doc_id: str) -> None:
        """Drop a subscription the push service reported as gone (404/410) —
        deleting one that's already gone is a no-op, not an error, so this
        is safe to call even if two prune passes race."""
        client = self._require_live()
        client.collection(self.collection).document(doc_id).delete()
