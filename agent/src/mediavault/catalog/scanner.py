"""
The scanner — walk a connector and write what it finds into the catalog.

Resumable by design. A full pass over a terabyte takes hours, and the one thing
that must not happen is a crash at 80% sending you back to zero. The scanner
commits a checkpoint after every directory, so an interrupted scan resumes from
the last directory it finished rather than the beginning.

Cost note: `list()` is cheap but carries no content hash, so the scanner calls
`stat()` per file to get one. That is a head-and-tail read per file and it is the
real cost of indexing — unavoidable, since dedup needs a fingerprint.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from ..ports import Connector, FileRecord
from .store import Catalog

#: `Connector.list` caps its own output; pass something larger than any real folder.
_NO_LIMIT = 1_000_000

#: A directory can hold thousands of files (a Google Drive folder in
#: particular -- seen over 1000 in one folder in practice), and every
#: upsert() in it shares one open write transaction until the directory
#: finishes (see checkpoint()'s docstring). That held the write lock open
#: for however long the *whole* directory took, which was long enough for
#: a concurrent writer (e.g. `publish` running at the same time -- WAL
#: mode + busy_timeout is meant to let that work, see Catalog.__init__)
#: to exceed its own wait and crash with "database is locked" instead of
#: just waiting its turn. Committing periodically instead of only at each
#: directory's end bounds how long any single held transaction can run.
_COMMIT_EVERY_N_FILES = 200

#: SQLite's own busy_timeout (30s, see Catalog.__init__) already waits out
#: most lock contention -- this is a second, coarser layer on top for the
#: rare case a wait still isn't enough (e.g. `index` and `publish` both
#: legitimately busy at once for longer than that). Retried a bounded
#: number of times with a real pause in between, same shape as the SMB
#: connector's own _retry() for a dropped session.
_LOCK_RETRY_ATTEMPTS = 3
_LOCK_RETRY_DELAY_SECONDS = 5

# OS/filesystem-generated housekeeping files, never actual photo/video content
# -- Windows' per-folder thumbnail cache, its per-folder view-settings file,
# macOS's per-folder metadata file, and Synology/QNAP's own "don't index me"
# marker. Matched by exact name (case-insensitive), not by extension: unlike
# real media (raw camera formats, obscure video containers), this is a small,
# stable, well-known set, so a narrow blacklist is safe where an allowlist of
# "real" extensions would risk excluding some legitimate format nobody's hit
# yet. These would otherwise get indexed like any other file and then fail
# every single publish attempt forever (Pillow can't decode them as an image),
# since nothing marks a permanently-undecodable item done -- see PublishAction's
# per-item retry-until-success docstring.
_JUNK_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", ".nomedia"}


@dataclass
class ScanProgress:
    directory: str
    files_seen: int
    files_indexed: int
    errors: int


@dataclass
class ScanReport:
    source: str
    files_indexed: int
    directories: int
    errors: int
    resumed_from: str
    error_samples: list[str]

    @property
    def ok(self) -> bool:
        return self.errors == 0


def _is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _upsert_with_retry(catalog: Catalog, source: str, record: FileRecord) -> None:
    """catalog.upsert(), retrying a bounded number of times if another
    writer (e.g. a concurrent `publish`) is holding the lock past
    busy_timeout -- see _LOCK_RETRY_ATTEMPTS' docstring above."""
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            catalog.upsert(source, record)
            return
        except sqlite3.OperationalError as e:
            if not _is_locked_error(e) or attempt == _LOCK_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_LOCK_RETRY_DELAY_SECONDS)


def walk_directories(connector: Connector, start: str = "",
                     on_list: Optional[Callable[[str], None]] = None) -> Iterator[str]:
    """Yield every directory under `start`, parents before children, sorted.

    Deterministic ordering is what makes the resume cursor meaningful: the same
    tree always produces the same sequence, so "resume after X" is well defined.

    on_list(directory), if given, fires right before each directory's
    children are listed — including directories `scan()` is *skipping* while
    fast-forwarding to a resume cursor. That skip phase re-walks (and
    re-lists) every directory before the cursor with otherwise zero progress
    output, so a hang during it looks identical to "resuming and nothing has
    happened yet" — this is what tells them apart (see GitHub #11).
    """
    stack = [start]
    while stack:
        current = stack.pop(0)
        yield current
        if on_list:
            on_list(current)
        children = []
        try:
            for record in connector.list(current, limit=_NO_LIMIT):
                if record.is_dir:
                    children.append(record.id)
        except Exception:
            # Broad for the same reason scan()'s own listing catch is --
            # a connector-specific error (e.g. Drive's HttpError) finding
            # this directory's subfolders must cost that one directory's
            # children, not the whole walk.
            continue
        stack = sorted(children) + stack


def scan(
    connector: Connector,
    catalog: Catalog,
    *,
    source: Optional[str] = None,
    resume: bool = True,
    on_progress: Optional[Callable[[ScanProgress], None]] = None,
    on_file: Optional[Callable[[str, int, int], None]] = None,
    on_list: Optional[Callable[[str], None]] = None,
) -> ScanReport:
    """Index every file a connector can see. Resumable, checkpointed per directory.

    on_file(item_id, seen, total_in_directory), if given, fires right before
    each file's stat() call — a live "what's it doing right now" hook. Useful
    because on_progress only fires once a whole directory finishes, which for
    a directory with hundreds/thousands of files can be many minutes with no
    signal at all of whether the scan is working or stuck (see GitHub #11).

    on_list(directory), if given, fires before listing each directory's
    children — see walk_directories()'s docstring. This covers the resume
    "skip phase" that on_file/on_progress don't: fast-forwarding to a cursor
    re-walks every prior directory with no other progress signal at all.
    """
    source = source or connector.name
    cursor = catalog.begin_scan(source, resume=resume)
    resumed_from = cursor

    files_indexed = 0
    directories = 0
    errors = 0
    error_samples: list[str] = []
    skipping = bool(cursor)

    for directory in walk_directories(connector, on_list=on_list):
        # Resume: fast-forward past everything already committed. The cursor holds
        # the last *finished* directory, so skipping stops once we pass it.
        if skipping:
            if directory == cursor:
                skipping = False
            continue

        directories += 1
        seen = 0
        indexed = 0

        try:
            listing = list(connector.list(directory, limit=_NO_LIMIT))
        except Exception as e:
            # Broad for the same reason the per-file catch below is -- a
            # transient/connector-specific error (e.g. a Drive API hiccup)
            # listing one directory must cost that directory, not the
            # whole scan.
            errors += 1
            if len(error_samples) < 10:
                error_samples.append(f"{directory}: {e}")
            continue

        file_records = [r for r in listing
                        if not r.is_dir and r.name.lower() not in _JUNK_NAMES]
        for record in file_records:
            seen += 1
            if on_file:
                on_file(record.id, seen, len(file_records))
            try:
                # list() carries no hash; stat() does the head/tail read.
                full = connector.stat(record.id)
                # Broad on purpose -- one file's failure must never crash a
                # multi-hour scan, and what it can raise varies by
                # connector: FileNotFoundError/PermissionError/OSError for
                # a filesystem, but Drive's own googleapiclient.errors.
                # HttpError for e.g. a native Google Doc with no binary
                # content to hash (real incident: this crashed an
                # overnight `index drive` run outright instead of being
                # logged and skipped like every other per-file problem).
                _upsert_with_retry(catalog, source, full)
                indexed += 1
                # Not just per-directory (see _COMMIT_EVERY_N_FILES) -- a
                # single Drive folder has run past 1000 files in practice,
                # which held one uncommitted transaction (and the write
                # lock with it) open for as long as that whole folder
                # took, long enough to collide with a concurrent `publish`
                # past its own busy_timeout wait.
                if indexed % _COMMIT_EVERY_N_FILES == 0:
                    catalog.conn.commit()
            except Exception as e:
                errors += 1
                if len(error_samples) < 10:
                    error_samples.append(f"{record.id}: {e}")

        files_indexed += indexed
        # Commit the directory and its cursor together — a crash costs one directory.
        catalog.checkpoint(source, directory, files_indexed)

        if on_progress:
            on_progress(ScanProgress(directory, seen, indexed, errors))

    catalog.finish_scan(source)
    return ScanReport(
        source=source,
        files_indexed=files_indexed,
        directories=directories,
        errors=errors,
        resumed_from=resumed_from,
        error_samples=error_samples,
    )
