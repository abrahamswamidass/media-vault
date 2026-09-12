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

from .. import faces, imaging, metadata
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
                 force: bool = False, mime_only: bool = False, debug: bool = False):
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
        # Restricts to items an index pass has already tagged with `mime` —
        # for a library where only part of a re-index has landed, so this
        # doesn't select items indexed_at-first that still have none. See
        # Catalog.unpublished()'s docstring.
        self.mime_only = mime_only
        # Prints each face's actual nearest-person distance and match/no-match
        # outcome — for seeing why clustering did or didn't group two photos
        # together, since that can't be answered by re-reading the code.
        self.debug = debug
        self._pending = None

    @property
    def target_id(self) -> str:
        return self.source

    @property
    def inputs(self) -> dict:
        return {"source": self.source, "connector": self.connector.name,
                "blobstore": self.blobs.name, "facts": self.facts.name,
                "max_items": self.max_items, "force": self.force,
                "mime_only": self.mime_only, "debug": self.debug}

    def validate(self) -> tuple[bool, str]:
        if self.catalog.count(self.source) == 0:
            return False, f"nothing indexed for {self.source} — run an index first"
        self._pending = self.catalog.unpublished(self.source, limit=self.max_items,
                                                  force=self.force, mime_only=self.mime_only)
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

        published, failed, skipped = [], [], []
        for row in self._pending:
            item_id = row["item_id"]

            # Extension-based, checked *before* ever touching the NAS --
            # not a recognized photo/video extension at all means this
            # isn't a decode failure worth retrying (a Google Takeout
            # .json metadata sidecar, an old Thumbs.db indexed before
            # scanner.py's _JUNK_NAMES existed, an iTunes backup's cache
            # files, ...). Deliberately NOT based on whether Pillow can
            # actually decode the bytes -- that conflated "not a photo"
            # with "a photo in a format this build can't decode yet" (a
            # raw camera file, say), silently and permanently giving up on
            # real photos instead of leaving them to retry. See
            # imaging.MEDIA_EXTENSIONS and Catalog.mark_skipped().
            suffix = PurePosixPath(item_id).suffix.lower()
            if suffix not in imaging.MEDIA_EXTENSIONS:
                reason = f"not a recognized photo/video extension ({suffix or 'none'})"
                self.catalog.mark_skipped(self.source, item_id, reason)
                self.catalog.conn.commit()
                skipped.append({"item_id": item_id, "error": reason})
                continue

            try:
                thumb_action = ThumbnailAction(item_id, self.connector, self.blobs)
                thumb = thumb_action.run(commit=True)
                if thumb.status == "failed":
                    if thumb.error and thumb.error.startswith("not found: "):
                        # The file's simply gone from the NAS now -- most
                        # often a web-module delete/archive from since the
                        # last index (the catalog is a cache, the NAS is
                        # truth; see store.py's own docstring), sometimes a
                        # manual move/rename. Either way, retrying a fixed
                        # path forever won't make it reappear -- a future
                        # re-index is what would notice it's back, if it
                        # ever is. Marked skipped, not failed, for the same
                        # reason a non-media extension is: nothing here
                        # will change on its own by asking again.
                        self.catalog.mark_skipped(self.source, item_id, thumb.error)
                        self.catalog.conn.commit()
                        skipped.append({"item_id": item_id, "error": thumb.error})
                    else:
                        failed.append({"item_id": item_id, "error": thumb.error})
                    continue
                # A "no-op" thumbnail (already stored) has no outputs — the key is
                # deterministic from the hash, so recompute it rather than skip.
                key = thumb.outputs.get("key") or blob_key(row["quick_hash"], "thumbs", "webp")

                # A fresh thumbnail derivation already read the file's own
                # bytes off the NAS (thumb_action.raw — None on a NoOp, which
                # reads nothing). Reused below for EXIF/phash/faces instead
                # of each paying for its own separate read of the same file
                # — what used to be up to 3 NAS reads (thumbnail, a dedicated
                # EXIF head-read, a dedicated full read for phash/faces) for
                # one newly-published item collapses to the 1 this loop
                # can't avoid anyway. `full` stays None here until/unless
                # something below still needs a read of its own (the NoOp
                # backfill case, e.g. a --force republish).
                full = thumb_action.raw

                # EXIF is a bonus, not a requirement — most exports/screenshots
                # have none, and PyExifTool/exiftool might not even be
                # installed in every deployment. Any failure here just means
                # this item's EXIF fields stay NULL, never blocks publishing.
                exif = {}
                try:
                    suffix = PurePosixPath(item_id).suffix
                    head = full[:_EXIF_HEAD_BYTES] if full is not None \
                        else self.connector.read(item_id, nbytes=_EXIF_HEAD_BYTES)
                    exif = metadata.extract(head, suffix=suffix)
                except Exception:
                    exif = {}
                if exif:
                    self.catalog.set_exif(self.source, item_id, exif)

                mime = row["mime"] or ""
                is_image = mime.startswith("image/")

                # Perceptual hash: a bonus, always-on for images (cheap
                # relative to face detection, no model/live-switch needed) —
                # for near-duplicate review grouping in the web module, see
                # imaging.phash(). An item published before this field
                # existed falls back to its own dedicated read below, once,
                # and is persisted so it's never paid for again after that.
                phash = row["phash"]
                need_phash = phash is None and is_image

                # Faces are a bonus too — never block publishing. Gated
                # behind FACES_LIVE (off by default, like every other live
                # switch here) since it costs real CPU time per image.
                # Idempotent per item: without the `not existing` check, a
                # `--force` re-run (e.g. to backfill GPS on already-published
                # items) would re-detect every face on every republished
                # item, duplicating rows in `faces` and re-paying the
                # compute cost for nothing new.
                #
                # bbox is normalized to a 0-1 fraction of the *full* image's
                # own dimensions (from EXIF, read above) before it ever
                # leaves this function -- the web only ever displays a
                # differently-sized thumbnail, never the full original, so a
                # raw pixel-coordinate box (what faces.detect_faces() and
                # the local `faces` table both store) would be meaningless
                # there. Only the box crosses to Firestore, never the
                # embedding that actually makes a face identifiable -- see
                # CLAUDE.md's face-detection design note.
                img_w, img_h = exif.get("width"), exif.get("height")

                def _norm_bbox(x1, y1, x2, y2):
                    if not img_w or not img_h:
                        return None
                    return [round(x1 / img_w, 4), round(y1 / img_h, 4),
                            round(x2 / img_w, 4), round(y2 / img_h, 4)]

                existing = self.catalog.faces_for_item(self.source, item_id)
                person_ids: list[str] = sorted({
                    str(f["person_id"]) for f in existing if f["person_id"] is not None})
                face_entries = [
                    {"person_id": str(f["person_id"]),
                     "bbox": _norm_bbox(f["bbox_x1"], f["bbox_y1"], f["bbox_x2"], f["bbox_y2"]),
                     "score": f["score"]}
                    for f in existing if f["person_id"] is not None
                ]
                need_faces = not existing and os.getenv("FACES_LIVE", "0") == "1" and is_image

                # `full` is already set above when the thumbnail step itself
                # read the file (the common case: a never-before-published
                # item). Only pay for a fresh read here on the NoOp-thumbnail
                # backfill path -- nothing has touched the NAS for this item
                # yet in that case.
                if full is None and (need_faces or need_phash):
                    try:
                        full = self.connector.read(item_id)
                    except Exception:
                        full = None

                if full is not None and need_phash:
                    try:
                        phash = imaging.phash(full)
                    except Exception:
                        pass

                if phash is not None:
                    self.catalog.set_phash(self.source, item_id, phash)

                if full is not None and need_faces:
                    on_match = None
                    if self.debug:
                        def on_match(best_id, best_dist, matched, _item_id=item_id):
                            dist_str = f"{best_dist:.3f}" if best_dist is not None else "n/a"
                            outcome = f"matched person {best_id}" if matched else "new person"
                            print(f"    face in {_item_id}: nearest dist {dist_str} "
                                  f"-> {outcome}", flush=True)
                    try:
                        for face in faces.detect_faces(full):
                            person_id = assign_person(self.catalog, face["embedding"], on_match=on_match)
                            self.catalog.add_face(
                                self.source, item_id, face["bbox"], face["score"],
                                face["embedding"], person_id)
                            person_ids.append(str(person_id))
                            face_entries.append({"person_id": str(person_id),
                                                 "bbox": _norm_bbox(*face["bbox"]),
                                                 "score": face["score"]})
                    except Exception:
                        person_ids = []
                        face_entries = []

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
                    "aperture": exif.get("aperture"),
                    "shutter_speed": exif.get("shutter_speed"),
                    "iso": exif.get("iso"),
                    "exposure_compensation": exif.get("exposure_compensation"),
                    "focal_length": exif.get("focal_length"),
                    "focal_length_35mm": exif.get("focal_length_35mm"),
                    "metering_mode": exif.get("metering_mode"),
                    "flash": exif.get("flash"),
                    "person_ids": person_ids,
                    "faces": face_entries,
                    "phash": phash,
                })
                self.catalog.mark_published(self.source, item_id)
                # Committed per item, not once at the end of the whole batch:
                # Firestore's fact just above already landed durably the
                # moment facts.put() returned, but the local catalog's own
                # bookkeeping (published_at, phash, faces) stayed invisible to
                # any other connection -- including a `stats` run in another
                # terminal -- until one giant commit after potentially
                # hundreds of items. That made a long FACES_LIVE=1 batch look
                # stalled even while it was actively working, and meant a
                # crash or OOM partway through lost every item's progress,
                # not just the one in flight. SQLite commits are cheap in
                # WAL mode, so there's no real cost to doing this every time.
                self.catalog.conn.commit()
                published.append(item_id)
            except Exception as e:
                failed.append({"item_id": item_id, "error": str(e)})

        if not published and not skipped:
            # A NoOp's outputs never reach the caller (Action.run() discards
            # them on this path) — without surfacing at least one real reason
            # here, "every item failed" and "nothing needed doing" print the
            # exact same message, with no way to tell which happened.
            if failed:
                sample = failed[0]
                raise NoOp(f"no items could be published — {len(failed)} failed, "
                          f"e.g. {sample['item_id']}: {sample['error']}")
            raise NoOp("no items could be published")
        # Skipped items did mutate the catalog (mark_published, so they stop
        # being retried) even though nothing was actually published for
        # them — the `not published and not skipped` check above is what
        # keeps that real work from being misreported as the no-op case.
        return {"published": len(published), "failed": failed, "skipped": skipped}
