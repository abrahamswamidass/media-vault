"""
Push original files to a cold-storage bucket for off-site backup.

Distinct from BlobStore's other use (derived thumbnails/previews — always
small, content-addressed, and gone after a day for previews): here the
*originals* go up, keyed by their NAS-relative path instead of a hash, so
the bucket stays human-browsable in the GCS Console a year from now.
Reuses the BlobStore port regardless — cold storage is nothing more than
"somewhere to put bytes and check if they're already there," just pointed
at a separate bucket (see cli.py's _coldstore_for). Streams via
Connector.read_chunks()/BlobStore.put_stream() rather than buffering a
whole file in memory, the same reasoning as dedup's full-content
confirmation: some of what lands here is multi-gigabyte video, and a
single read() risks an unrecoverable OOM kill instead of a catchable error.
"""
from __future__ import annotations

from ..ports import BlobStore, Connector
from .base import Action, NoOp


def cold_key(item_id: str) -> str:
    """The object key for one NAS-relative path in the cold bucket. Mirrors
    the path (not content-addressed) on purpose — see blobstore.py's
    docstring on why thumbnails ARE hash-keyed; an archive meant to be
    browsed later should read like the folder it came from."""
    return f"archive/{item_id.lstrip('/')}"


class ColdArchiveAction(Action):
    """Upload one NAS original to cold storage. Leaves the NAS copy in
    place — this is a backup, not a move (see MoveAction in file_ops.py
    for that shape, if offloading NAS space is ever wanted later)."""
    action_type = "cold_archive"

    def __init__(self, item_id: str, connector: Connector, coldstore: BlobStore,
                 catalog=None):
        self.item_id = item_id
        self.connector = connector
        self.coldstore = coldstore
        self.catalog = catalog
        self._key = cold_key(item_id)

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"connector": self.connector.name, "coldstore": self.coldstore.name,
                "item_id": self.item_id, "key": self._key}

    def validate(self) -> tuple[bool, str]:
        try:
            rec = self.connector.stat(self.item_id)
        except FileNotFoundError:
            return False, f"not found: {self.item_id}"
        if rec.is_dir:
            return False, f"{self.item_id} is a directory"
        return True, ""

    def describe(self) -> str:
        return f"upload {self.item_id} from {self.connector.name} -> {self.coldstore.url(self._key)}"

    def _execute(self) -> dict:
        # The catalog's cold_archived_at is what the CLI loop uses to build
        # its worklist in the first place, but this checks the bucket
        # itself too before uploading — a crash between the upload and the
        # catalog commit (mid-batch, thousands of files) would otherwise
        # re-upload a file that's already there on the very next run.
        if self.coldstore.exists(self._key):
            if self.catalog:
                self.catalog.mark_cold_archived(self.connector.name, self.item_id)
                self.catalog.conn.commit()
            raise NoOp(f"already in cold storage: {self._key}")
        self.coldstore.put_stream(self._key, self.connector.read_chunks(self.item_id))
        if self.catalog:
            self.catalog.mark_cold_archived(self.connector.name, self.item_id)
            self.catalog.conn.commit()
        return {"key": self._key}
