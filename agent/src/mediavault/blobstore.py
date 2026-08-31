"""
Blob storage adapters — where thumbnails and previews land.

The `BlobStore` port itself lives in `ports.py` alongside `Connector`; this file
holds the implementations. Same Ports & Adapters move as `connectors`: one small
interface, several implementations. `LocalBlobStore` makes the whole thumbnail
and full-res pipeline runnable and testable with no cloud account at all;
`GCSBlobStore` is the real one.

**Keys are content-addressed.** A blob is named by the hash of the file it came
from — `thumbs/<hash>.webp`, not `thumbs/Photos/2026-01/img_001.webp`. That one
choice buys three things:

  * uploads are idempotent — re-running a scan re-derives the same key, and
    `exists()` short-circuits the upload
  * duplicates collapse — two copies of a photo share one blob
  * renames are free — moving a file on the NAS changes no blob, only a pointer
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .ports import BlobStore


#: A connector's `quick_hash` looks like "12345:abcdef..." — not URL-safe, and the
#: size prefix adds nothing once hashed. Normalise to a bare hex key.
_HASH_RE = re.compile(r"[^a-zA-Z0-9]")


def blob_key(quick_hash: str, kind: str, ext: str) -> str:
    """Build the content-addressed key for a derived blob.

    >>> blob_key("15423:a9f3c1", "thumbs", "webp")
    'thumbs/15423a9f3c1.webp'
    """
    digest = _HASH_RE.sub("", quick_hash)
    return f"{kind}/{digest}.{ext.lstrip('.')}"



class LocalBlobStore(BlobStore):
    """Writes blobs to a folder. Used by tests and by `--offline` runs."""
    name = "local"

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError(f"refusing to write outside blob root: {p}")
        return p

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def url(self, key: str) -> str:
        return self._path(key).as_uri()


class GCSBlobStore(BlobStore):
    """Google Cloud Storage — the real target for thumbnails and previews.

    Ships guarded, the same way `DriveConnector` does: the shape is real and
    callable today, the network calls stay behind an explicit env check so a
    half-configured machine can never surprise you with a bill or a write.

    To go live:
      1. pip install google-cloud-storage
      2. point GOOGLE_APPLICATION_CREDENTIALS at a service-account key
      3. set GCS_BUCKET and GCS_LIVE=1

    Set a **lifecycle rule** on the bucket deleting `previews/` after 1 day.
    Thumbnails are permanent; full-res previews are disposable by design, and
    that rule is the only thing keeping them from accumulating.
    """
    name = "gcs"

    def __init__(self, bucket: str | None = None):
        self.bucket_name = bucket or os.getenv("GCS_BUCKET", "")
        self.live = os.getenv("GCS_LIVE", "0") == "1"
        self._bucket = None

    def _require_live(self):
        if not self.live:
            raise NotImplementedError(
                "GCS is in SAFE mode (GCS_LIVE!=1). Use LocalBlobStore, or set "
                "GCS_BUCKET + GCS_LIVE=1 once credentials are in place."
            )
        if not self.bucket_name:
            raise NotImplementedError("GCS_LIVE=1 but GCS_BUCKET is not set.")
        if self._bucket is None:
            from google.cloud import storage  # optional extra — only imported when GCS_LIVE=1

            self._bucket = storage.Client().bucket(self.bucket_name)
        return self._bucket

    def exists(self, key: str) -> bool:
        bucket = self._require_live()
        return bucket.blob(key).exists()

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        bucket = self._require_live()
        blob = bucket.blob(key)
        blob.upload_from_string(data, content_type=content_type)
        return key

    def put_stream(self, key: str, chunks, content_type: str = "application/octet-stream") -> str:
        """Overrides the base class's buffer-then-put() default — writes
        each chunk straight to the wire via a resumable upload session
        instead of holding a whole file (cold storage's originals include
        multi-gigabyte video, unlike this store's usual thumbnails) in
        memory first."""
        bucket = self._require_live()
        blob = bucket.blob(key)
        with blob.open("wb", content_type=content_type) as f:
            for chunk in chunks:
                f.write(chunk)
        return key

    def url(self, key: str) -> str:
        return f"gs://{self.bucket_name}/{key}"
