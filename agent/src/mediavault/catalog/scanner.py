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

from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from ..ports import Connector, FileRecord
from .store import Catalog

#: `Connector.list` caps its own output; pass something larger than any real folder.
_NO_LIMIT = 1_000_000


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
        except (FileNotFoundError, ValueError, PermissionError):
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
        except (FileNotFoundError, ValueError, PermissionError) as e:
            errors += 1
            if len(error_samples) < 10:
                error_samples.append(f"{directory}: {e}")
            continue

        file_records = [r for r in listing if not r.is_dir]
        for record in file_records:
            seen += 1
            if on_file:
                on_file(record.id, seen, len(file_records))
            try:
                # list() carries no hash; stat() does the head/tail read.
                full = connector.stat(record.id)
                catalog.upsert(source, full)
                indexed += 1
            except (FileNotFoundError, ValueError, PermissionError, OSError) as e:
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
