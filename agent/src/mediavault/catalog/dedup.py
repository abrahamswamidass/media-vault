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
from typing import Callable, Optional

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


def _full_hash_cached(catalog: Optional[Catalog], connector: Connector,
                      source: str, row: sqlite3.Row) -> str:
    """Reuse a full hash already confirmed in a prior run, if one is on file.

    `full_hash` is a real column on `items`, auto-invalidated by `Catalog.upsert`
    whenever `quick_hash` changes (i.e. the file's content actually changed on
    re-index) — so a cached value here is always for the *current* content, no
    separate staleness check needed. Without this, every dedup run re-reads and
    re-hashes every candidate from zero regardless of what a prior run already
    confirmed — the expensive, network-bound part of confirmation.
    """
    cached = row["full_hash"]
    if cached:
        return cached
    computed = _full_hash(connector, row["item_id"])
    if catalog is not None:
        catalog.set_full_hash(source, row["item_id"], computed)
    return computed


def find_duplicates(
    catalog: Catalog,
    source: str,
    connector: Optional[Connector] = None,
    *,
    confirm: bool = True,
    min_size: int = 1,
    on_confirm: Optional[Callable[[int, int, str], None]] = None,
) -> list[DuplicateGroup]:
    """Find exact-duplicate groups within one source and choose each survivor.

    With `confirm=True` (the default) every group larger than the quick-hash
    coverage window is verified by fully hashing its members, and any member whose
    content actually differs is dropped from the group rather than archived. That
    verification needs a live `connector`; without one, groups come back
    unconfirmed and no action will archive them.

    on_confirm(done, total, keeper_item_id), if given, fires right before each
    group that actually needs a full-content read — not every group, since
    anything at or under the quick-hash coverage window is confirmed for
    free. On a library where most files exceed that window, confirmation can
    mean thousands of full-file reads with otherwise zero progress output —
    the same silent-but-working problem `index --debug` solved for scanning.
    """
    groups: list[DuplicateGroup] = []
    raw_groups = catalog.duplicate_groups(source, min_size=min_size)

    # All members of a group share one size (it's embedded in quick_hash
    # itself), so this needs no sorting — just a cheap in-memory pre-count,
    # no I/O — to give on_confirm a real total instead of an unknown "?/?".
    to_confirm_total = 0
    if on_confirm and confirm and connector is not None:
        to_confirm_total = sum(
            1 for rows in raw_groups
            if len(rows) >= 2 and (rows[0]["size"] or 0) > FULLY_COVERED_BY_QUICK_HASH
        )
    to_confirm = 0

    for rows in raw_groups:
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
            to_confirm += 1
            if on_confirm:
                on_confirm(to_confirm, to_confirm_total, keeper["item_id"])
            group = _confirm(group, connector, catalog=catalog, source=source)

        groups.append(group)

    return sorted(groups, key=lambda g: g.reclaimable_bytes, reverse=True)


def _confirm(group: DuplicateGroup, connector: Connector, *,
             catalog: Optional[Catalog] = None, source: str = "") -> DuplicateGroup:
    """Fully hash every member; drop any that isn't genuinely identical.

    Files can share a size and both end windows and still differ in the middle —
    unlikely for photos, entirely plausible for video and documents with fixed
    headers and footers. This is the check that stands between that case and a
    deleted file.

    Catches any exception, not just the obvious file-I/O ones. A connector's
    own transient-connection errors (e.g. SMBConnectionClosed, or the SPNEGO
    auth error smbclient's own reconnect can surface) aren't OSError subclasses,
    so a narrower catch here missed them entirely — by the time one reaches
    this function, the connector has already exhausted its own retry/reconnect
    attempts, so treating it as "can't confirm this one, move on" is the
    correct, safe response, not a crash of the whole confirmation pass.

    With a `catalog` given, each hash is persisted (and reused, if already
    present) via `_full_hash_cached` — see its docstring for why that's safe.
    """
    try:
        keeper_hash = _full_hash_cached(catalog, connector, source, group.keeper)
    except Exception as e:
        group.confirmed = False
        group.confirm_note = f"could not read keeper: {e}"
        return group

    identical, differing = [], []
    for row in group.losers:
        try:
            if _full_hash_cached(catalog, connector, source, row) == keeper_hash:
                identical.append(row)
            else:
                differing.append(row)
        except Exception as e:
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


def folder_breakdown(groups: list[DuplicateGroup], depth: int = 3) -> list[dict]:
    """Where the reclaimable space actually sits, bucketed by the first
    `depth` path segments of each redundant copy's location.

    A large dedup run (thousands of groups) is unreadable one group at a
    time — this answers the more useful question first: which folders
    account for most of the duplication, before committing to archiving
    any of it. Only counts archivable (confirmed, safe_to_archive) groups,
    same as `summarize()`.
    """
    buckets: dict[str, dict] = {}
    for g in groups:
        if not g.safe_to_archive:
            continue
        for row in g.losers:
            parts = row["item_id"].split("/")
            folder = "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1])
            folder = folder or "/"
            b = buckets.setdefault(folder, {"folder": folder, "copies": 0, "reclaimable_bytes": 0})
            b["copies"] += 1
            b["reclaimable_bytes"] += row["size"] or 0
    return sorted(buckets.values(), key=lambda b: b["reclaimable_bytes"], reverse=True)
