"""
Library-wide actions — the ones that operate on a whole source rather than one file.

    IndexAction        walk a source into the catalog. Resumable.
    DedupSourceAction  find identical copies within a source and archive the extras.
    PublishAction      push a thumbnail + metadata fact for every un-published item.

All three are Actions rather than loose functions so they inherit the same dry-run
gate and the same journal entry as everything else. A publish run triggered from
the web module leaves exactly the record a publish run typed at the terminal does.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Optional

from .. import faces, metadata
from ..blobstore import blob_key
from ..catalog import assign_person, dedup as dedup_mod, scanner
from ..catalog.store import Catalog
from ..ports import BlobStore, Connector, FactsStore
from .base import Action, NoOp
from .dedup import ArchiveDuplicatesAction
from .derive import ThumbnailAction

#: EXIF lives near the start of a file — a small HEAD read is enough (same
#: "never read a whole file" philosophy as quick_hash), and far cheaper than
#: the full read ThumbnailAction needs for actual pixel decoding.
_EXIF_HEAD_BYTES = 1_048_576


class IndexAction(Action):
    """Walk one source into the catalog, resuming any interrupted pass."""
    action_type = "index"

    def __init__(self, source: str, connector: Connector, catalog: Catalog,
                 restart: bool = False):
        self.source = source
        self.connector = connector
        self.catalog = catalog
        self.restart = restart

    @property
    def target_id(self) -> str:
        return self.source

    @property
    def inputs(self) -> dict:
        return {"source": self.source, "connector": self.connector.name,
                "restart": self.restart}

    def validate(self) -> tuple[bool, str]:
        try:
            next(iter(self.connector.list("", limit=1)), None)
        except (FileNotFoundError, ValueError, PermissionError) as e:
            return False, f"cannot read {self.connector.name}: {e}"
        return True, ""

    def describe(self) -> str:
        state = self.catalog.scan_state(self.source)
        if state and not state["complete"] and not self.restart:
            return (f"resume indexing {self.source} from {state['cursor'] or 'the start'} "
                    f"({state['items_seen']:,} files already recorded)")
        return f"index {self.source} from the beginning"

    def _execute(self) -> dict:
        report = scanner.scan(self.connector, self.catalog, source=self.source,
                              resume=not self.restart)
        return {
            "files_indexed": report.files_indexed,
            "directories": report.directories,
            "errors": report.errors,
            "resumed_from": report.resumed_from,
            "error_samples": report.error_samples,
        }


class DedupSourceAction(Action):
    """Archive every confirmed duplicate within one source.

    Composes `ArchiveDuplicatesAction` once per group, so each group is validated
    independently and one bad group cannot take the rest down with it.

    Never compares across sources. The NAS holding everything and Drive holding a
    curated copy of the good things means cross-source overlap is correct, and the
    grouping query is scoped to one source precisely so that overlap is unreachable.
    """
    action_type = "dedup_source"

    def __init__(self, source: str, connector: Connector, catalog: Catalog,
                 *, confirm: bool = True, min_size: int = 1,
                 max_groups: Optional[int] = None):
        self.source = source
        self.connector = connector
        self.catalog = catalog
        self.confirm = confirm
        self.min_size = min_size
        self.max_groups = max_groups
        self._groups = None

    @property
    def target_id(self) -> str:
        return self.source

    @property
    def inputs(self) -> dict:
        return {"source": self.source, "connector": self.connector.name,
                "confirm": self.confirm, "min_size": self.min_size,
                "max_groups": self.max_groups}

    def validate(self) -> tuple[bool, str]:
        if not self.connector.can_delete:
            return False, f"{self.connector.name} cannot archive files"
        if self.catalog.count(self.source) == 0:
            return False, f"nothing indexed for {self.source} — run an index first"

        groups = dedup_mod.find_duplicates(
            self.catalog, self.source, self.connector,
            confirm=self.confirm, min_size=self.min_size)
        self._groups = [g for g in groups if g.safe_to_archive]
        if self.max_groups is not None:
            self._groups = self._groups[: self.max_groups]
        return True, ""

    def describe(self) -> str:
        groups = self._groups or []
        copies = sum(len(g.losers) for g in groups)
        freed = sum(g.reclaimable_bytes for g in groups)
        if not groups:
            return f"no confirmed duplicates to archive in {self.source}"
        return (f"archive {copies} redundant "
                f"{'copy' if copies == 1 else 'copies'} across {len(groups)} "
                f"group(s) in {self.source}, reclaiming {freed:,} bytes "
                f"(one copy of each is always kept)")

    def _execute(self) -> dict:
        if not self._groups:
            raise NoOp(f"no confirmed duplicates in {self.source}")

        archived, failed, freed = [], [], 0
        for group in self._groups:
            result = ArchiveDuplicatesAction(
                group, self.connector, self.catalog).run(commit=True)
            if result.status == "ok":
                archived.append({"kept": result.outputs.get("kept"),
                                 "archived": len(result.outputs.get("archived", []))})
                freed += result.outputs.get("bytes_reclaimed", 0)
            else:
                failed.append({"kept": group.keeper["item_id"], "error": result.error})

        if not archived:
            raise NoOp("no groups could be archived")
        return {"groups_archived": len(archived), "bytes_reclaimed": freed,
                "failed": failed, "detail": archived}


class PublishAction(Action):
    """Push a thumbnail + metadata fact for every catalog item not yet published.

    Composes `ThumbnailAction` once per item, same shape `DedupSourceAction` uses
    for `ArchiveDuplicatesAction` — one item failing doesn't take the rest down.

    An item is marked published in the catalog only after BOTH the thumbnail and
    the fact land, so a crash mid-run just leaves that item unpublished for the
    next pass to retry. Content-addressed blob keys make the thumbnail step
    idempotent too — re-running finds it already there and moves straight to
    writing the fact.
    """
    action_type = "publish"

    def __init__(self, source: str, connector: Connector, catalog: Catalog,
                 blobs: BlobStore, facts: FactsStore, *, max_items: Optional[int] = None,
                 force: bool = False):
        self.source = source
        self.connector = connector
        self.catalog = catalog
        self.blobs = blobs
        self.facts = facts
        self.max_items = max_items
        # Re-processes already-published items too — for backfilling a fact
        # field added after they were first published (e.g. GPS), without a
        # full reset + re-index. Thumbnails are unaffected: content-addressed
        # and unchanged, ThumbnailAction's own idempotency check still skips
        # re-deriving one that's already there.
        self.force = force
        self._pending = None

    @property
    def target_id(self) -> str:
        return self.source

    @property
    def inputs(self) -> dict:
        return {"source": self.source, "connector": self.connector.name,
                "blobstore": self.blobs.name, "facts": self.facts.name,
                "max_items": self.max_items, "force": self.force}

    def validate(self) -> tuple[bool, str]:
        if self.catalog.count(self.source) == 0:
            return False, f"nothing indexed for {self.source} — run an index first"
        self._pending = self.catalog.unpublished(self.source, limit=self.max_items,
                                                  force=self.force)
        return True, ""

    def describe(self) -> str:
        n = len(self._pending or [])
        if not n:
            return f"nothing to publish in {self.source} — already up to date"
        verb = "republish" if self.force else "publish"
        return (f"{verb} {n} item(s) from {self.source}: "
                f"thumbnail -> {self.blobs.name}, metadata -> {self.facts.name}")

    def _execute(self) -> dict:
        if not self._pending:
            raise NoOp(f"nothing to publish in {self.source}")

        published, failed = [], []
        for row in self._pending:
            item_id = row["item_id"]
            try:
                thumb = ThumbnailAction(item_id, self.connector, self.blobs).run(commit=True)
                if thumb.status == "failed":
                    failed.append({"item_id": item_id, "error": thumb.error})
                    continue
                # A "no-op" thumbnail (already stored) has no outputs — the key is
                # deterministic from the hash, so recompute it rather than skip.
                key = thumb.outputs.get("key") or blob_key(row["quick_hash"], "thumbs", "webp")

                # EXIF is a bonus, not a requirement — most exports/screenshots
                # have none, and PyExifTool/exiftool might not even be
                # installed in every deployment. Any failure here just means
                # this item's EXIF fields stay NULL, never blocks publishing.
                exif = {}
                try:
                    suffix = PurePosixPath(item_id).suffix
                    head = self.connector.read(item_id, nbytes=_EXIF_HEAD_BYTES)
                    exif = metadata.extract(head, suffix=suffix)
                except Exception:
                    exif = {}
                if exif:
                    self.catalog.set_exif(self.source, item_id, exif)

                # Faces, like EXIF, are a bonus — never block publishing.
                # Gated behind FACES_LIVE (off by default, like every other
                # live switch here) since the first real run needs to
                # download model weights and costs real CPU time per image.
                # Images only: insightface can't do anything useful with a
                # video frame, and this needs the FULL file (unlike EXIF's
                # bounded head read) since a face model has to decode the
                # whole image, not peek at its header.
                #
                # Idempotent per item: without this check, a `--force`
                # re-run (e.g. to backfill GPS on already-published items)
                # would re-detect every face on every republished item,
                # duplicating rows in `faces` and re-paying the compute cost
                # for nothing new.
                existing = self.catalog.faces_for_item(self.source, item_id)
                person_ids: list[str] = sorted({
                    str(f["person_id"]) for f in existing if f["person_id"] is not None})
                mime = row["mime"] or ""
                if not existing and os.getenv("FACES_LIVE", "0") == "1" and mime.startswith("image/"):
                    try:
                        full = self.connector.read(item_id)
                        for face in faces.detect_faces(full):
                            person_id = assign_person(self.catalog, face["embedding"])
                            self.catalog.add_face(
                                self.source, item_id, face["bbox"], face["score"],
                                face["embedding"], person_id)
                            person_ids.append(str(person_id))
                    except Exception:
                        person_ids = []

                self.facts.put(self.source, item_id, {
                    "source": self.source, "item_id": item_id, "name": row["name"],
                    "size": row["size"], "mtime": row["mtime"], "mime": row["mime"],
                    "quick_hash": row["quick_hash"], "thumbnail_key": key,
                    "thumbnail_url": self.blobs.url(key),
                    "width": exif.get("width"), "height": exif.get("height"),
                    "date_taken": exif.get("date_taken"),
                    "camera_make": exif.get("camera_make"),
                    "camera_model": exif.get("camera_model"),
                    "latitude": exif.get("latitude"),
                    "longitude": exif.get("longitude"),
                    "duration_seconds": exif.get("duration_seconds"),
                    "person_ids": person_ids,
                })
                self.catalog.mark_published(self.source, item_id)
                published.append(item_id)
            except Exception as e:
                failed.append({"item_id": item_id, "error": str(e)})

        if published:
            self.catalog.conn.commit()

        if not published:
            # A NoOp's outputs never reach the caller (Action.run() discards
            # them on this path) — without surfacing at least one real reason
            # here, "every item failed" and "nothing needed doing" print the
            # exact same message, with no way to tell which happened.
            if failed:
                sample = failed[0]
                raise NoOp(f"no items could be published — {len(failed)} failed, "
                          f"e.g. {sample['item_id']}: {sample['error']}")
            raise NoOp("no items could be published")
        return {"published": len(published), "failed": failed}
