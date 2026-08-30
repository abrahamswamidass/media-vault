"""
Everyday file actions: delete, copy, move.

Each is a handful of lines because `Action.run()` owns the dry-run gate, the
timing, and the error trapping. What's left here is only the part that differs
between one operation and the next.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ..ports import Connector, FactsStore
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

    Also flips the local catalog's state back to 'active' when a catalog is
    given (optional, same reason as ArchiveItemAction's) — upsert()'s own
    ON CONFLICT clause never touches `state`, so without this a restored
    item would stay invisible to dedup/publish forever, even once it's back
    at its original location. This does NOT bring the item back to the web
    viewer by itself: that needs a `publish --force` afterward to re-derive
    the fact (a full re-publish, not just this state flip, since the old
    fact was already removed by ArchiveItemAction) — see the tracking issue.
    """
    action_type = "restore"

    def __init__(self, item_id: str, connector: Connector, catalog=None):
        self.item_id = item_id
        self.connector = connector
        self.catalog = catalog

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
        try:
            res = self.connector.restore(self.item_id, commit=True)
        except FileNotFoundError:
            # Nothing in trash to restore. Either this item was never
            # soft-deleted, or — the case this guards against — a replayed
            # "restore" intent: the web module's Undo button has no way to
            # know a previous click already succeeded (its disabled state
            # doesn't survive a page revisit), and at-least-once intent
            # delivery means the same intent can also be re-claimed after a
            # crash. Either way the end state the caller wants — this item
            # existing at its original location — already holds, so this is
            # a clean no-op, not a failure to report as broken in Activity.
            raise NoOp(f"nothing to restore: {self.item_id}")
        if not res.ok:
            raise RuntimeError(res.detail)
        if self.catalog:
            self.catalog.mark_active(self.connector.name, self.item_id)
            self.catalog.conn.commit()
        return res.data


class ArchiveItemAction(Action):
    """Soft-delete a file AND remove its published Firestore fact — what
    the web module's "delete" intent actually needs (see sync/intents.py),
    since a plain DeleteAction only moves the file: the item would keep
    showing in Browse/Map/Folders/People/Duplicates forever, because
    nothing else ever cleans up a fact once it's been published. Plain
    DeleteAction is still what MoveAction and the raw `nas delete` CLI
    passthrough use — this is specifically the composed, web-facing version.

    Ordered fact-delete-then-file-delete, not the other way round: both
    steps are independently safe to retry (Firestore/local fact deletion is
    already a no-op on something already gone; DeleteAction's own
    validate() fails clearly if the file's not there). If the file-delete
    step fails after the fact is already gone, the photo is briefly missing
    from the web view but the file itself is untouched and safe on the NAS
    — the safer direction to fail in than the reverse (file moved to trash,
    but a stuck fact keeps showing it as if nothing happened).

    Also updates the local catalog's state to 'archived', same as
    ArchiveDuplicatesAction does for exact-dedup archiving — without it, a
    web-archived item would still read as `state='active'` to a later
    `dedup`/`publish` pass, which would then try (and safely fail, but
    noisily) to read a file that's no longer at its catalog path. `catalog`
    is optional, matching ArchiveDuplicatesAction's own precedent — mainly
    so tests can exercise this without needing a full catalog fixture.
    """
    action_type = "delete"

    def __init__(self, item_id: str, connector: Connector, facts: FactsStore,
                 catalog=None):
        self.item_id = item_id
        self.connector = connector
        self.facts = facts
        self.catalog = catalog
        self._delete = DeleteAction(item_id, connector)

    @property
    def target_id(self) -> str:
        return self.item_id

    @property
    def inputs(self) -> dict:
        return {"connector": self.connector.name, "facts": self.facts.name,
                "item_id": self.item_id}

    def validate(self) -> tuple[bool, str]:
        return self._delete.validate()

    def describe(self) -> str:
        return f"{self._delete.describe()}, and remove its published fact"

    def _execute(self) -> dict:
        self.facts.delete(self.connector.name, self.item_id)
        outputs = self._delete._execute()
        if self.catalog:
            self.catalog.mark_archived(self.connector.name, self.item_id)
            self.catalog.conn.commit()
        return outputs


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
