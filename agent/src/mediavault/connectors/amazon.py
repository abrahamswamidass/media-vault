"""
Amazon connector — write-only, "drop into a watched folder" pattern.

We deliberately DON'T talk to Amazon's (unofficial) API. Instead the agent copies
cherry-picked files into a staging folder that Amazon's official desktop app
watches and auto-uploads. So 'upload' here just means "stage the file"; Amazon's
own app owns the actual upload. Fire-and-forget, zero fragile code.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..ports import Connector, FileRecord, OpResult, NotSupported
from .nas import NASConnector


class AmazonConnector(Connector):
    name = "amazon"
    can_delete = False
    can_upload = True

    def __init__(self, staging_root: str, album_by_month: bool = True):
        self.album_by_month = album_by_month
        self._fs = NASConnector(staging_root, trash=str(Path(staging_root) / "__never_used_trash"))

    def list(self, prefix: str = "", limit: int = 100):
        # Lets you inspect what's currently staged/waiting for the app to pick up.
        for rec in self._fs.list(prefix, limit):
            rec.source = self.name
            yield rec

    def stat(self, item_id: str) -> FileRecord:
        rec = self._fs.stat(item_id)
        rec.source = self.name
        return rec

    def upload(self, local_path: str, dest: str = "", commit: bool = False) -> OpResult:
        """Stage a file into a dated album subfolder the Amazon app will auto-upload."""
        if not dest:
            album = datetime.now().strftime("%Y-%m") if self.album_by_month else ""
            dest = str(Path(album) / Path(local_path).name) if album else Path(local_path).name
        res = self._fs.upload(local_path, dest, commit=commit)
        res.action = "stage-for-amazon"
        if commit:
            res.detail = f"staged for Amazon auto-upload -> {res.data.get('dest')}"
        else:
            res.detail = res.detail.replace("upload", "stage-for-amazon")
        return res
