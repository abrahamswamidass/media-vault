"""
Everyday file actions: delete, copy, move.

Each is a handful of lines because `Action.run()` owns the dry-run gate, the
timing, and the error trapping. What's left here is only the part that differs
between one operation and the next.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ..ports import Connector
from .base import Action, NoOp


class DeleteAction(Action):
    """Soft-delete an item — on the NAS this moves it to the trash folder.

    Reversible by design: the connector decides what "delete" means, and the NAS
    adapter deliberately implements it as a move rather than an unlink.
    """
    action_type = "delete"

    def __init__(self, item_id: str, connector: Connector):
        self.item_id = item_id
        self.connector = connector

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"connector": self.connector.name, "item_id": self.item_id}

    def validate(self) -> tuple[bool, str]:
        if not self.connector.can_delete:
            return False, f"{self.connector.name} does not support delete"
        try:
            self.connector.stat(self.item_id)
        except FileNotFoundError:
            return False, f"not found: {self.item_id}"
        return True, ""

    def describe(self) -> str:
        return f"delete {self.item_id} from {self.connector.name} (soft — moves to trash)"

    def _execute(self) -> dict:
        res = self.connector.delete(self.item_id, commit=True)
        if not res.ok:
            raise RuntimeError(res.detail)
        return res.data


class RestoreAction(Action):
    """Undo a soft delete — the reverse of DeleteAction. Moves a file back
    out of trash to where it came from (NAS), or clears Drive's own trashed
    flag. Every connector's delete() in this project is deliberately
    reversible, so anything that can be deleted can be restored.

    Unlike DeleteAction, validate() can't stat() the item_id at its
    original location — that's exactly where it no longer is, having been
    moved to trash. connector.restore() itself raises FileNotFoundError if
    nothing's actually there to restore.
    """
    action_type = "restore"

    def __init__(self, item_id: str, connector: Connector):
        self.item_id = item_id
        self.connector = connector

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"connector": self.connector.name, "item_id": self.item_id}

    def validate(self) -> tuple[bool, str]:
        if not self.connector.can_delete:
            return False, f"{self.connector.name} does not support delete/restore"
        return True, ""

    def describe(self) -> str:
        return f"restore {self.item_id} on {self.connector.name} (undo a soft delete)"

    def _execute(self) -> dict:
        res = self.connector.restore(self.item_id, commit=True)
        if not res.ok:
            raise RuntimeError(res.detail)
        return res.data


class CopyAction(Action):
    """Copy an item from one connector to another. Source is left untouched."""
    action_type = "copy"

    def __init__(self, item_id: str, source: Connector, dest: Connector,
                 dest_path: str = ""):
        self.item_id = item_id
        self.source = source
        self.dest = dest
        self.dest_path = dest_path

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"source": self.source.name, "dest": self.dest.name,
                "item_id": self.item_id, "dest_path": self.dest_path}

    def validate(self) -> tuple[bool, str]:
        if not self.dest.can_upload:
            return False, f"{self.dest.name} does not accept uploads"
        try:
            rec = self.source.stat(self.item_id)
        except FileNotFoundError:
            return False, f"source not found: {self.item_id}"
        if rec.is_dir:
            return False, f"{self.item_id} is a directory — copy one file at a time"
        return True, ""

    def describe(self) -> str:
        where = self.dest_path or Path(self.item_id).name
        return f"copy {self.item_id} from {self.source.name} to {self.dest.name}:{where}"

    def _execute(self) -> dict:
        # Connectors expose read()->bytes and upload(local_path), so a cross-connector
        # copy lands in a temp file in between. Fine for photos; revisit if we ever
        # need to move files too large to hold in memory.
        data = self.source.read(self.item_id)
        # Always name the destination explicitly. A connector asked to upload with
        # no destination falls back to the *local* filename, which here would be
        # the temp file's — landing the photo as "tmp8f3k2a.jpg".
        dest_path = self.dest_path or Path(self.item_id).name
        with tempfile.NamedTemporaryFile(suffix=Path(self.item_id).suffix,
                                         delete=False) as tmp:
            tmp.write(data)
            staged = tmp.name
        try:
            res = self.dest.upload(staged, dest=dest_path, commit=True)
            if not res.ok:
                raise RuntimeError(res.detail)
            return res.data
        finally:
            Path(staged).unlink(missing_ok=True)


class MoveAction(Action):
    """Copy to the destination, then soft-delete the source.

    Ordered so a failure mid-way leaves the file present in at least one place —
    never neither. If the copy succeeds and the delete fails, you get a duplicate,
    which is the recoverable direction to fail in.
    """
    action_type = "move"

    def __init__(self, item_id: str, source: Connector, dest: Connector,
                 dest_path: str = ""):
        self.item_id = item_id
        self.source = source
        self.dest = dest
        self.dest_path = dest_path
        self._copy = CopyAction(item_id, source, dest, dest_path)
        self._delete = DeleteAction(item_id, source)

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"source": self.source.name, "dest": self.dest.name,
                "item_id": self.item_id, "dest_path": self.dest_path}

    def validate(self) -> tuple[bool, str]:
        for step in (self._copy, self._delete):
            ok, reason = step.validate()
            if not ok:
                return False, reason
        return True, ""

    def describe(self) -> str:
        where = self.dest_path or Path(self.item_id).name
        return f"move {self.item_id} to {self.dest.name}:{where}, then trash the original"

    def _execute(self) -> dict:
        copied = self._copy._execute()
        deleted = self._delete._execute()
        return {"copied": copied, "deleted": deleted}
