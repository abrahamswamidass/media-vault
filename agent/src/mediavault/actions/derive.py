"""
Derived-blob actions — the two things the agent pushes to the cloud.

    ThumbnailAction     bulk, agent-initiated. Runs over the whole library once,
                        then only over what changed. Permanent.

    FetchFullResAction  on demand, answering an intent from the web module.
                        You clicked a photo; this puts something worth looking at
                        where the browser can reach it. Expires in a day.

Both write content-addressed blobs and both HEAD-check first, so re-running either
one costs a metadata lookup and nothing else. That's what makes at-least-once
intent delivery safe: a replayed fetch finds the blob already there and reports
no-op instead of re-reading the NAS.
"""
from __future__ import annotations

from pathlib import Path

from .. import imaging
from ..blobstore import blob_key
from ..ports import BlobStore, Connector
from .base import Action, NoOp


def _read_decodable(connector: Connector, item_id: str, mime: str) -> bytes:
    """Raw bytes ready for Pillow. For video, that's one representative
    frame (see imaging.frame()) rather than the container itself, which
    Pillow can't open at all."""
    raw = connector.read(item_id)
    if (mime or "").startswith("video/"):
        return imaging.frame(raw, suffix=Path(item_id).suffix)
    return raw


class ThumbnailAction(Action):
    """Derive one 400px thumbnail and push it to the blob store."""
    action_type = "thumbnail"

    def __init__(self, item_id: str, connector: Connector, blobs: BlobStore,
                 force: bool = False):
        self.item_id = item_id
        self.connector = connector
        self.blobs = blobs
        self.force = force          # re-derive even if the blob already exists
        self._record = None         # cached stat() from validate()

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"connector": self.connector.name, "item_id": self.item_id,
                "blobstore": self.blobs.name, "force": self.force}

    def validate(self) -> tuple[bool, str]:
        try:
            rec = self.connector.stat(self.item_id)
        except FileNotFoundError:
            return False, f"not found: {self.item_id}"
        if rec.is_dir:
            return False, f"{self.item_id} is a directory"
        if not rec.quick_hash:
            return False, f"no content hash for {self.item_id} — cannot address the blob"
        self._record = rec
        return True, ""

    @property
    def _key(self) -> str:
        return blob_key(self._record.quick_hash, "thumbs", "webp")

    def describe(self) -> str:
        return f"thumbnail {self.item_id} -> {self.blobs.name}:{self._key}"

    def _execute(self) -> dict:
        key = self._key
        if not self.force and self.blobs.exists(key):
            raise NoOp(f"thumbnail already stored: {key}")
        decodable = _read_decodable(self.connector, self.item_id, self._record.mime)
        data = imaging.thumbnail(decodable)
        self.blobs.put(key, data, content_type="image/webp")
        return {"key": key, "bytes": len(data), "url": self.blobs.url(key)}


class FetchFullResAction(Action):
    """Put a full-resolution view of one item where the browser can fetch it.

    The web module never reaches into the NAS. When you want to actually look at
    a photo, it files an intent and this action answers it — reading the original
    at home, pushing a viewable copy up, and reporting back the key.

    Two variants:

        "preview"   2048px JPEG, ~500 KB. The default, and what you want for
                    deciding whether to keep something.
        "original"  the untouched bytes. For downloading the real file — a 50 MB
                    RAW costs about half a cent in egress, so it's fine
                    occasionally and wrong as a default.

    Blobs land under `previews/`, which the bucket's lifecycle rule empties after
    a day. Nothing here accumulates; you pay for what you actually opened.
    """
    action_type = "fetch_fullres"

    #: Variants this action accepts, mapped to (blob extension, MIME type).
    VARIANTS = {
        "preview": ("jpg", "image/jpeg"),
        "original": (None, "application/octet-stream"),   # extension taken from the item
    }

    def __init__(self, item_id: str, connector: Connector, blobs: BlobStore,
                 variant: str = "preview"):
        self.item_id = item_id
        self.connector = connector
        self.blobs = blobs
        self.variant = variant
        self._record = None

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"connector": self.connector.name, "item_id": self.item_id,
                "blobstore": self.blobs.name, "variant": self.variant}

    def validate(self) -> tuple[bool, str]:
        if self.variant not in self.VARIANTS:
            return False, (f"unknown variant {self.variant!r} — "
                           f"expected one of {sorted(self.VARIANTS)}")
        try:
            rec = self.connector.stat(self.item_id)
        except FileNotFoundError:
            return False, f"not found: {self.item_id}"
        if rec.is_dir:
            return False, f"{self.item_id} is a directory"
        if not rec.quick_hash:
            return False, f"no content hash for {self.item_id} — cannot address the blob"
        self._record = rec
        return True, ""

    @property
    def _key(self) -> str:
        ext, _ = self.VARIANTS[self.variant]
        if ext is None:                                  # "original" keeps its own suffix
            ext = (self._record.name.rsplit(".", 1) + ["bin"])[1]
        return blob_key(self._record.quick_hash, f"previews/{self.variant}", ext)

    def describe(self) -> str:
        return (f"fetch {self.variant} of {self.item_id} "
                f"-> {self.blobs.name}:{self._key} (expires in 1 day)")

    def _execute(self) -> dict:
        key = self._key
        # Already up there from an earlier look? Then this costs one HEAD and we
        # never touch the NAS. This is the idempotency guarantee for replayed intents.
        if self.blobs.exists(key):
            raise NoOp(f"already available: {key}")

        _, content_type = self.VARIANTS[self.variant]
        if self.variant == "preview":
            decodable = _read_decodable(self.connector, self.item_id, self._record.mime)
            data = imaging.preview(decodable)
        else:
            data = self.connector.read(self.item_id)
            content_type = self._record.mime or content_type

        self.blobs.put(key, data, content_type=content_type)
        return {"key": key, "bytes": len(data), "variant": self.variant,
                "url": self.blobs.url(key)}
