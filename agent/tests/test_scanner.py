"""
Regression coverage for the scanner's junk-file filter.

Thumbs.db (and its cousins) used to get indexed like any other file and then
fail every single publish attempt forever -- Pillow can't decode a folder
thumbnail cache as an image, and nothing marks a permanently-undecodable item
done, so it silently re-consumed a slot in every future publish batch. See
scanner.py's `_JUNK_NAMES` docstring.
"""
from __future__ import annotations

import argparse
import sqlite3

import pytest

from mediavault.catalog import Catalog, scanner
from mediavault.connectors.nas import NASConnector
from mediavault.ports import Connector, FileRecord


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def catalog(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as c:
        yield c


def test_known_junk_files_are_never_indexed(tmp_path, catalog):
    nas = tmp_path / "nas"
    _write(nas, "img.jpg", b"pretend-jpeg" * 100)
    _write(nas, "Thumbs.db", b"windows folder thumbnail cache")
    _write(nas, "desktop.ini", b"[.ShellClassInfo]")
    _write(nas, ".DS_Store", b"macos folder metadata")
    _write(nas, "sub/Thumbs.db", b"nested copy too")

    scanner.scan(NASConnector(str(nas)), catalog, source="nas")

    assert catalog.count("nas") == 1
    assert catalog.get("nas", "img.jpg") is not None
    assert catalog.get("nas", "Thumbs.db") is None
    assert catalog.get("nas", "desktop.ini") is None
    assert catalog.get("nas", ".DS_Store") is None
    assert catalog.get("nas", "sub/Thumbs.db") is None


def test_junk_filter_is_case_insensitive(tmp_path, catalog):
    nas = tmp_path / "nas"
    _write(nas, "img.jpg", b"pretend-jpeg" * 100)
    _write(nas, "THUMBS.DB", b"windows folder thumbnail cache, oddly cased")

    scanner.scan(NASConnector(str(nas)), catalog, source="nas")

    assert catalog.count("nas") == 1
    assert catalog.get("nas", "THUMBS.DB") is None


# --------------------------------------------------------------------------- #
# Resilience regression: a real overnight `index drive` run crashed outright
# on a connector-specific error (googleapiclient's HttpError, for a native
# Google Doc with no binary content) and separately on a concurrent `publish`
# holding the write lock past busy_timeout ("database is locked") -- both
# used to take the whole multi-hour scan down instead of costing one file.
# --------------------------------------------------------------------------- #
class _FakeConnector(Connector):
    """A minimal Connector whose stat() can be told to blow up for specific
    ids with an arbitrary exception -- narrower fakes (NASConnector against
    real files) can't simulate a connector-specific error like Drive's own
    HttpError, which isn't a subclass of anything filesystem-related."""
    name = "fake"

    def __init__(self, records: list[FileRecord], *, raise_for: dict[str, Exception] = None):
        self._records = records
        self._raise_for = raise_for or {}

    def list(self, prefix: str = "", limit: int = 100) -> list[FileRecord]:
        if prefix:
            return []
        return self._records[:limit]

    def stat(self, item_id: str) -> FileRecord:
        if item_id in self._raise_for:
            raise self._raise_for[item_id]
        return next(r for r in self._records if r.id == item_id)


class _NotAnOSError(Exception):
    """Stands in for googleapiclient.errors.HttpError -- the actual bug was
    that it isn't a subclass of FileNotFoundError/ValueError/PermissionError/
    OSError, so the old narrow except clause never caught it at all."""


def test_a_connector_specific_error_is_caught_per_file_not_crashing_the_scan(catalog):
    """Regression: an overnight `index drive` crashed outright on a native
    Google Doc ("Only files with binary content can be downloaded") --
    one file's error took the whole multi-hour scan down instead of being
    logged and skipped like every other per-file problem already is."""
    records = [
        FileRecord(id="a.jpg", name="a.jpg", source="fake", size=10, mtime=0),
        FileRecord(id="Some Doc", name="Some Doc", source="fake", size=None, mtime=0),
        FileRecord(id="b.jpg", name="b.jpg", source="fake", size=10, mtime=0),
    ]
    conn = _FakeConnector(records, raise_for={
        "Some Doc": _NotAnOSError("Only files with binary content can be downloaded"),
    })

    report = scanner.scan(conn, catalog, source="fake")  # must not raise

    assert report.files_indexed == 2
    assert report.errors == 1
    assert "Some Doc" in report.error_samples[0]
    assert catalog.get("fake", "a.jpg") is not None
    assert catalog.get("fake", "b.jpg") is not None
    assert catalog.get("fake", "Some Doc") is None


def test_upsert_retries_and_recovers_from_a_locked_database(catalog, monkeypatch):
    monkeypatch.setattr(scanner, "_LOCK_RETRY_DELAY_SECONDS", 0)
    record = FileRecord(id="a.jpg", name="a.jpg", source="fake", size=10, mtime=0,
                        quick_hash="h")
    real_upsert = catalog.upsert
    attempts = {"n": 0}

    def flaky_upsert(source, rec):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_upsert(source, rec)
    catalog.upsert = flaky_upsert

    scanner._upsert_with_retry(catalog, "fake", record)

    assert attempts["n"] == 2
    assert catalog.get("fake", "a.jpg") is not None


def test_upsert_retry_gives_up_after_the_lock_never_clears(catalog, monkeypatch):
    monkeypatch.setattr(scanner, "_LOCK_RETRY_DELAY_SECONDS", 0)
    record = FileRecord(id="a.jpg", name="a.jpg", source="fake", size=10, mtime=0)
    catalog.upsert = lambda source, rec: (_ for _ in ()).throw(
        sqlite3.OperationalError("database is locked"))

    with pytest.raises(sqlite3.OperationalError):
        scanner._upsert_with_retry(catalog, "fake", record)


def test_a_non_locked_operational_error_is_not_retried(catalog, monkeypatch):
    """Only "database is locked" is treated as retryable -- any other
    sqlite3.OperationalError is a real problem, not lock contention, and
    should surface immediately."""
    monkeypatch.setattr(scanner, "_LOCK_RETRY_DELAY_SECONDS", 0)
    record = FileRecord(id="a.jpg", name="a.jpg", source="fake", size=10, mtime=0)
    calls = {"n": 0}

    def bad_upsert(source, rec):
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: items")
    catalog.upsert = bad_upsert

    with pytest.raises(sqlite3.OperationalError):
        scanner._upsert_with_retry(catalog, "fake", record)
    assert calls["n"] == 1  # no retry wasted on a non-lock error


def test_a_large_directory_commits_periodically_not_just_at_the_end(tmp_path, monkeypatch):
    """A Google Drive folder has run past 1000 files in practice -- every
    upsert() in one directory used to share a single open transaction
    until that whole directory finished, holding the write lock open long
    enough to collide with a concurrent `publish` past its own
    busy_timeout wait. Committing periodically bounds how long any one
    held transaction can run -- proven here the way it actually matters:
    a second, independent connection to the same file can already see the
    first batch's rows mid-directory, not just after the whole 25-file
    directory (and its own end-of-directory checkpoint) finishes."""
    monkeypatch.setattr(scanner, "_COMMIT_INTERVAL_SECONDS", 0)
    records = [
        FileRecord(id=f"{i}.jpg", name=f"{i}.jpg", source="fake", size=10, mtime=0)
        for i in range(25)
    ]
    conn = _FakeConnector(records)
    db_path = str(tmp_path / "cat.sqlite")
    seen_mid_scan = {}

    def on_file(item_id, seen, total):
        # Fires right *before* each file's stat()/upsert() -- at seen == 11,
        # files 1-10 are done, so the periodic commit at 10 should already
        # have landed, and file 11 itself hasn't touched the catalog yet.
        if seen == 11:
            with Catalog(db_path) as other_connection:
                seen_mid_scan["count"] = other_connection.count("fake")

    with Catalog(db_path) as catalog:
        scanner.scan(conn, catalog, source="fake", on_file=on_file)
        assert catalog.count("fake") == 25

    assert seen_mid_scan.get("count") == 10
