"""
The archive-duplicates action.

Takes one confirmed duplicate group and archives every copy except the survivor.
"Archive" means whatever reversible deletion the connector offers — the NAS moves
the file to its trash folder, Drive moves it to Drive's 30-day trash. Nothing here
can permanently erase anything.

Guard rails, all enforced in `validate()` before a byte moves:

  * the group must be **confirmed** — an unverified fingerprint match never archives
  * the keeper must still exist, and must not appear among the losers
  * every loser must still exist, or the group is stale and the scan should re-run
  * a group with nothing to archive is refused rather than silently succeeding
"""
from __future__ import annotations

from typing import Optional

from ..catalog.dedup import DuplicateGroup
from ..catalog.store import Catalog
from ..ports import Connector
from .base import Action, NoOp


class ArchiveDuplicatesAction(Action):
    """Archive the redundant copies in one confirmed duplicate group."""
    action_type = "archive_duplicates"

    def __init__(self, group: DuplicateGroup, connector: Connector,
                 catalog: Optional[Catalog] = None):
        self.group = group
        self.connector = connector
        self.catalog = catalog

    @property
    def target_id(self) -> str:
        # The keeper identifies the group: it is the one file guaranteed to survive.
        return self.group.keeper["item_id"]

    @property
    def inputs(self) -> dict:
        return {
            "connector": self.connector.name,
            "source": self.group.source,
            "quick_hash": self.group.quick_hash,
            "keeper": self.group.keeper["item_id"],
            "losers": [r["item_id"] for r in self.group.losers],
            "keeper_reason": self.group.keeper_reason,
        }

    def validate(self) -> tuple[bool, str]:
        g = self.group

        if not self.connector.can_delete:
            return False, f"{self.connector.name} cannot archive files"
        if not g.confirmed:
            return False, f"group not confirmed ({g.confirm_note or 'no verification run'})"
        if not g.losers:
            return False, "nothing to archive — group has a single surviving copy"

        keeper_id = g.keeper["item_id"]
        loser_ids = [r["item_id"] for r in g.losers]
        if keeper_id in loser_ids:
            return False, f"keeper {keeper_id} also listed for archiving — refusing"

        # The catalog is a cache. If it has drifted from the mount, the group is
        # stale and acting on it could archive the last remaining copy.
        try:
            self.connector.stat(keeper_id)
        except FileNotFoundError:
            return False, f"keeper no longer exists: {keeper_id} — re-run the scan"

        for item_id in loser_ids:
            try:
                self.connector.stat(item_id)
            except FileNotFoundError:
                return False, f"duplicate no longer exists: {item_id} — re-run the scan"

        return True, ""

    def describe(self) -> str:
        g = self.group
        return (f"keep {g.keeper['item_id']} ({g.keeper_reason}), "
                f"archive {len(g.losers)} identical "
                f"{'copy' if len(g.losers) == 1 else 'copies'} "
                f"({g.reclaimable_bytes:,} bytes) from {self.connector.name}")

    def _execute(self) -> dict:
        archived, failed = [], []

        for row in self.group.losers:
            item_id = row["item_id"]
            try:
                result = self.connector.delete(item_id, commit=True)
                if result.ok:
                    archived.append({"item_id": item_id,
                                     "dest": result.data.get("dest", ""),
                                     "bytes": row["size"] or 0})
                    if self.catalog:
                        self.catalog.mark_archived(self.group.source, item_id)
                else:
                    failed.append({"item_id": item_id, "error": result.detail})
            except Exception as e:
                failed.append({"item_id": item_id, "error": str(e)})

        if self.catalog and archived:
            self.catalog.conn.commit()

        if not archived:
            raise NoOp("no copies were archived")
        if failed:
            # Partial success is still progress — report it rather than pretending
            # the whole group failed. The keeper is untouched either way.
            return {"kept": self.group.keeper["item_id"], "archived": archived,
                    "failed": failed,
                    "bytes_reclaimed": sum(a["bytes"] for a in archived)}

        return {"kept": self.group.keeper["item_id"], "archived": archived,
                "bytes_reclaimed": sum(a["bytes"] for a in archived)}
