"""
Stage an item from any connector for Amazon — the other half of issue #8.

`CopyAction` (file_ops.py) always supplies an explicit dest_path so a
cross-connector copy doesn't land under some temp file's throwaway name — the
right default for `nas`/`drive`/`archive`, which just want the original
filename preserved. `AmazonConnector.upload()` has its own, better default
for Amazon specifically (a dated `YYYY-MM/` album subfolder), which only
kicks in when dest is left empty. This action calls it that way instead of
reusing CopyAction, which would silently skip the dated-album behavior.

Depending on the concrete `AmazonConnector` (not just the `Connector` port,
the way every other action does) is a deliberate, narrow exception — the
dated-album default is Amazon-specific, not something the general port
interface expresses.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ..connectors.amazon import AmazonConnector
from ..ports import Connector
from .base import Action


class StageForAmazonAction(Action):
    """Copy one item from any source connector into Amazon's dated staging
    folder — no local file needed first, unlike `mediavault amazon upload`."""
    action_type = "stage_for_amazon"

    def __init__(self, item_id: str, source: Connector, amazon: AmazonConnector):
        self.item_id = item_id
        self.source = source
        self.amazon = amazon

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"source": self.source.name, "item_id": self.item_id}

    def validate(self) -> tuple[bool, str]:
        try:
            rec = self.source.stat(self.item_id)
        except FileNotFoundError:
            return False, f"source not found: {self.item_id}"
        if rec.is_dir:
            return False, f"{self.item_id} is a directory — stage one file at a time"
        return True, ""

    def describe(self) -> str:
        return f"stage {self.item_id} from {self.source.name} for Amazon (dated album)"

    def _execute(self) -> dict:
        data = self.source.read(self.item_id)
        # AmazonConnector.upload() derives the dated-album filename from
        # local_path's own basename, so the temp file must be named after the
        # ORIGINAL item, not a random tempfile name — otherwise a random name
        # like "tmp55g30dp3.jpg" lands in the staging folder instead of the
        # photo's real name.
        with tempfile.TemporaryDirectory() as tmpdir:
            staged = Path(tmpdir) / Path(self.item_id).name
            staged.write_bytes(data)
            # dest intentionally omitted — AmazonConnector.upload() builds the
            # dated album path itself when dest is empty.
            res = self.amazon.upload(str(staged), commit=True)
            if not res.ok:
                raise RuntimeError(res.detail)
            return res.data
