"""
Archive connector — read-only indexer for exported dumps.

Google Photos (Takeout) and Amazon ("Request My Data" / bulk web download) are
locked-down clouds: you can't index them live, so you export a folder and point
this connector at it. It reuses the NAS reader under the hood and additionally
picks up Takeout-style JSON metadata sidecars when present.

By design there is NO delete/upload here — an export is a read-only snapshot.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ..base import Connector, FileRecord, NotSupported
from .nas import NASConnector


class ArchiveConnector(Connector):
    name = "archive"
    can_delete = False
    can_upload = False

    def __init__(self, root: str):
        # Lean on the NAS reader; an export is just a folder tree.
        self._fs = NASConnector(root, trash=str(Path(root) / "__never_used_trash"))

    def list(self, prefix: str = "", limit: int = 100) -> Iterable[FileRecord]:
        for rec in self._fs.list(prefix, limit):
            rec.source = self.name
            yield rec

    def stat(self, item_id: str) -> FileRecord:
        rec = self._fs.stat(item_id)
        rec.source = self.name
        # Takeout drops a "<file>.json" (or "<file>.supplemental-metadata.json") sidecar.
        for cand in (item_id + ".json",
                     item_id + ".supplemental-metadata.json"):
            side = self._fs.root / cand
            if side.exists():
                try:
                    rec.extra["takeout"] = json.loads(side.read_text(encoding="utf-8"))
                except Exception as e:
                    rec.extra["takeout_error"] = str(e)
                break
        return rec

    def read(self, item_id: str, nbytes: int = 0) -> bytes:
        return self._fs.read(item_id, nbytes)
