"""
Core contracts shared by every connector.

The whole harness is built around ONE idea: every source (NAS, Drive, an
exported archive, the Amazon staging folder) exposes the same small set of
verbs. That way the CLI can test any operation against any connector with
identical syntax, and the real agent later reuses these exact classes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional
import time


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #
@dataclass
class FileRecord:
    """A single item as seen by a connector. Not every field is always known."""
    id: str                       # connector-native id (NAS: path, Drive: fileId)
    name: str
    source: str                   # "nas" | "drive" | "archive" | "amazon"
    size: Optional[int] = None    # bytes
    mtime: Optional[float] = None # epoch seconds
    mime: Optional[str] = None
    is_dir: bool = False
    quick_hash: Optional[str] = None   # cheap fingerprint (size + head/tail)
    extra: dict = field(default_factory=dict)  # raw metadata blob (exif, etc.)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OpResult:
    """Uniform result envelope so the CLI can print success/failure the same way."""
    ok: bool
    action: str                   # "delete" | "upload" | ...
    target: str
    committed: bool               # False => dry-run, nothing actually changed
    detail: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class NotSupported(Exception):
    """Raised when a connector doesn't implement an operation (e.g. delete on a read-only archive)."""


# --------------------------------------------------------------------------- #
# The blob port — where derived images go
# --------------------------------------------------------------------------- #
class BlobStore(ABC):
    """Somewhere to put derived bytes and get them back by key.

    Peer of `Connector`: connectors read your originals, blob stores hold the
    thumbnails and previews made from them. Adapters live in `blobstore.py`.
    """
    name: str = "blobstore"

    @abstractmethod
    def exists(self, key: str) -> bool:
        """True if this key is already stored. Cheap — a HEAD, not a download."""

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Store bytes under key. Returns the key. Overwrites if present."""

    @abstractmethod
    def url(self, key: str) -> str:
        """A reference the web module can resolve. Not necessarily public."""


# --------------------------------------------------------------------------- #
# The facts port — where cataloged metadata goes for the web module to read
# --------------------------------------------------------------------------- #
class FactsStore(ABC):
    """Somewhere to put one metadata document per catalog item (Firestore).

    Peer of `BlobStore`: blob stores hold the derived bytes, facts stores hold
    the structured metadata pointing at them. Per CLAUDE.md's write-ownership
    rule, this is the agent's side of "intents in, facts out" — the agent is
    the only writer here. Adapters live in `sync/facts.py`.
    """
    name: str = "facts"

    @abstractmethod
    def put(self, source: str, item_id: str, fact: dict) -> None:
        """Write (overwrite) the metadata document for one item."""

    @abstractmethod
    def purge(self, source: str | None = None) -> int:
        """Delete every fact for a source (or everything if source is None).

        Testing/dev convenience — mirrors Catalog.reset(). Needed because facts
        are keyed by item_id (a path), so re-indexing under a different scan
        root (e.g. a subfolder -> the whole share) produces new keys for the
        same files, leaving the old documents stale rather than overwritten.
        Returns the number of documents deleted.
        """


# --------------------------------------------------------------------------- #
# The interface every connector implements
# --------------------------------------------------------------------------- #
class Connector(ABC):
    name: str = "base"
    can_delete: bool = False
    can_upload: bool = False

    # --- read side -------------------------------------------------------- #
    @abstractmethod
    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        """List items under a path/prefix. Should be cheap and streamable."""

    @abstractmethod
    def stat(self, item_id: str) -> FileRecord:
        """Full metadata for a single item."""

    def read(self, item_id: str, nbytes: int = 0) -> bytes:
        """Return file bytes. nbytes=0 => whole file; >0 => just the first N (safe peek)."""
        raise NotSupported(f"{self.name}: read not implemented")

    # --- write side (guarded) -------------------------------------------- #
    def delete(self, item_id: str, commit: bool = False) -> OpResult:
        raise NotSupported(f"{self.name}: delete not supported")

    def upload(self, local_path: str, dest: str = "", commit: bool = False) -> OpResult:
        raise NotSupported(f"{self.name}: upload not supported")

    # --- helpers ---------------------------------------------------------- #
    def dryrun_result(self, action: str, target: str, detail: str = "", **data) -> OpResult:
        return OpResult(ok=True, action=action, target=target, committed=False,
                        detail=f"DRY-RUN (no change): {detail}", data=data)
