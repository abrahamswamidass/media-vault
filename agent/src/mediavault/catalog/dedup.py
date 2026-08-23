"""
Duplicate detection — within one source, never across.

**The rule that shapes this whole file:** the NAS holds everything and Drive holds
a curated copy of the good things. A photo existing in both is the system working
correctly. So duplicate groups are built inside a single source and there is no
code path anywhere here that can compare NAS against Drive.

Two tiers of match, with very different consequences:

    exact   identical bytes, confirmed by a full hash.  Safe to auto-archive.
    near    perceptually similar (a re-compression, a resize).  Review only.

Only the exact tier is implemented as an automatic action. A near-duplicate pair
often differs in ways that matter — one is the full-resolution original and one is
a messenger-app copy — and picking wrong destroys the better file. That decision
belongs to a person, so near-duplicates surface as something to look at, never as
something already done.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..ports import Connector
from .store import Catalog

#: `quick_fingerprint` hashes the first and last 64 KB. At or below 128 KB those
#: two windows cover every byte, so quick_hash IS a full hash and needs no
#: confirmation. Above it, the middle is unexamined and we read the file properly
#: before deleting anything.
FULLY_COVERED_BY_QUICK_HASH = 2 * 65536


@dataclass
class DuplicateGroup:
    """One set of identical files, with the survivor already chosen."""
    source: str
    quick_hash: str
    keeper: sqlite3.Row
    losers: list[sqlite3.Row]
    keeper_reason: str
    confirmed: bool = False
    confirm_note: str = ""
    _split: list[sqlite3.Row] = field(default_factory=list)   # members that differed

    @property
    def reclaimable_bytes(self) -> int:
        return sum(r["size"] or 0 for r in self.losers)

    @property
    def safe_to_archive(self) -> bool:
        return self.confirmed and bool(self.losers)

    def describe(self) -> str:
        return (f"{len(self.losers) + 1} copies of {self.keeper['name']} — "
                f"keep {self.keeper['item_id']} ({self.keeper_reason}), "
                f"archive {len(self.losers)}")


def _keeper_rank(row: sqlite3.Row) -> tuple:
    """Sort key for choosing the survivor. Lowest wins.

    Oldest first (the original, not a later copy), then the shallowest path
    (`Photos/x.jpg` beats `Photos/old/backup/x.jpg`), then alphabetical so the
    outcome is fully deterministic and re-running proposes the same thing.
    """
    return (
        row["mtime"] if row["mtime"] is not None else float("inf"),
        row["item_id"].count("/"),
        row["item_id"],
    )


def _keeper_reason(keeper: sqlite3.Row, others: list[sqlite3.Row]) -> str:
    if any((o["mtime"] or float("inf")) > (keeper["mtime"] or float("inf")) for o in others):
        return "oldest copy"
    if any(o["item_id"].count("/") > keeper["item_id"].count("/") for o in others):
        return "shallowest path"
    return "alphabetically first"


def _full_hash(connector: Connector, item_id: str) -> str:
    """SHA-256 of the entire file. Only ever called on duplicate candidates."""
    h = hashlib.sha256()
    h.update(connector.read(item_id))
    return h.hexdigest()


def find_duplicates(
    catalog: Catalog,
    source: str,
    connector: Optional[Connector] = None,
    *,
    confirm: bool = True,
    min_size: int = 1,
) -> list[DuplicateGroup]:
    """Find exact-duplicate groups within one source and choose each survivor.

    With `confirm=True` (the default) every group larger than the quick-hash
    coverage window is verified by fully hashing its members, and any member whose
    content actually differs is dropped from the group rather than archived. That
    verification needs a live `connector`; without one, groups come back
    unconfirmed and no action will archive them.
    """
    groups: list[DuplicateGroup] = []

    for rows in catalog.duplicate_groups(source, min_size=min_size):
        if len(rows) < 2:
            continue

        ordered = sorted(rows, key=_keeper_rank)
        keeper, losers = ordered[0], ordered[1:]
        group = DuplicateGroup(
            source=source,
            quick_hash=keeper["quick_hash"],
            keeper=keeper,
            losers=losers,
            keeper_reason=_keeper_reason(keeper, losers),
        )

        size = keeper["size"] or 0
        if size <= FULLY_COVERED_BY_QUICK_HASH:
            # Head and tail windows already overlap the whole file.
            group.confirmed = True
            group.confirm_note = "quick hash covers every byte at this size"
        elif not confirm:
            group.confirm_note = "unconfirmed — confirmation was disabled"
        elif connector is None:
            group.confirm_note = "unconfirmed — no connector available to read contents"
        else:
            group = _confirm(group, connector)

        groups.append(group)

    return sorted(groups, key=lambda g: g.reclaimable_bytes, reverse=True)


def _confirm(group: DuplicateGroup, connector: Connector) -> DuplicateGroup:
    """Fully hash every member; drop any that isn't genuinely identical.

    Files can share a size and both end windows and still differ in the middle —
    unlikely for photos, entirely plausible for video and documents with fixed
    headers and footers. This is the check that stands between that case and a
    deleted file.
    """
    try:
        keeper_hash = _full_hash(connector, group.keeper["item_id"])
    except (FileNotFoundError, OSError, ValueError) as e:
        group.confirmed = False
        group.confirm_note = f"could not read keeper: {e}"
        return group

    identical, differing = [], []
    for row in group.losers:
        try:
            if _full_hash(connector, row["item_id"]) == keeper_hash:
                identical.append(row)
            else:
                differing.append(row)
        except (FileNotFoundError, OSError, ValueError) as e:
            differing.append(row)
            group.confirm_note = f"could not read {row['item_id']}: {e}"

    group.losers = identical
    group._split = differing
    group.confirmed = True
    if differing:
        group.confirm_note = (
            f"{len(differing)} member(s) shared a fingerprint but differ in content "
            f"— left alone"
        )
    elif not group.confirm_note:
        group.confirm_note = "verified by full content hash"
    return group


def summarize(groups: list[DuplicateGroup]) -> dict:
    """Headline numbers for a dedup run."""
    archivable = [g for g in groups if g.safe_to_archive]
    return {
        "groups": len(groups),
        "archivable_groups": len(archivable),
        "redundant_copies": sum(len(g.losers) for g in archivable),
        "reclaimable_bytes": sum(g.reclaimable_bytes for g in archivable),
        "unconfirmed_groups": len([g for g in groups if not g.confirmed]),
        "split_by_verification": sum(len(g._split) for g in groups),
    }
