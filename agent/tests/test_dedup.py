"""
Dedup safety tests.

These guard the invariants that stand between an automatic archive and lost data:
duplicates are only ever compared within one source, a fingerprint match is never
enough on its own, and every group keeps exactly one copy.
"""
from __future__ import annotations

import os

import pytest

from mediavault.actions import STATUS_FAILED, STATUS_NOOP, STATUS_OK
from mediavault.actions.dedup import ArchiveDuplicatesAction
from mediavault.actions.maintenance import DedupSourceAction, IndexAction
from mediavault.catalog import Catalog, find_duplicates, scan, summarize
from mediavault.connectors.nas import NASConnector

#: Bigger than the 128 KB the quick hash actually covers, so these files reach the
#: full-content verification path rather than being trusted on fingerprint alone.
BIG = b"HEADER" + bytes(range(256)) * 900
TAIL = BIG[-70000:]


def _write(root, rel, data, mtime=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def nas(tmp_path):
    root = tmp_path / "nas"
    root.mkdir()
    return root


@pytest.fixture
def catalog(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as c:
        yield c


def _indexed(root, catalog, source="nas"):
    conn = NASConnector(str(root))
    scan(conn, catalog, source=source)
    return conn


# --------------------------------------------------------------------------- #
# Grouping and keeper choice
# --------------------------------------------------------------------------- #
def test_keeps_the_oldest_copy(nas, catalog):
    _write(nas, "Photos/img.jpg", BIG, mtime=1_000_000)
    _write(nas, "Backup/img.jpg", BIG, mtime=2_000_000)
    _write(nas, "Downloads/img (1).jpg", BIG, mtime=3_000_000)
    conn = _indexed(nas, catalog)

    groups = find_duplicates(catalog, "nas", conn)

    assert len(groups) == 1
    assert groups[0].keeper["item_id"] == "Photos/img.jpg"
    assert groups[0].keeper_reason == "oldest copy"
    assert len(groups[0].losers) == 2


def test_ties_break_on_shallowest_path(nas, catalog):
    _write(nas, "img.jpg", BIG, mtime=1_000_000)
    _write(nas, "a/b/c/img.jpg", BIG, mtime=1_000_000)
    conn = _indexed(nas, catalog)

    group = find_duplicates(catalog, "nas", conn)[0]

    assert group.keeper["item_id"] == "img.jpg"
    assert group.keeper_reason == "shallowest path"


def test_unique_files_are_not_grouped(nas, catalog):
    _write(nas, "a.jpg", BIG)
    _write(nas, "b.jpg", b"something else entirely" * 5000)
    conn = _indexed(nas, catalog)

    assert find_duplicates(catalog, "nas", conn) == []


# --------------------------------------------------------------------------- #
# The safety invariants
# --------------------------------------------------------------------------- #
def test_sources_are_never_compared(nas, tmp_path, catalog):
    """The same photo on NAS and in Drive is correct — Drive is the curated copy."""
    _write(nas, "Photos/img.jpg", BIG)
    drive_root = tmp_path / "drive"
    _write(drive_root, "img.jpg", BIG)

    scan(NASConnector(str(nas)), catalog, source="nas")
    scan(NASConnector(str(drive_root)), catalog, source="drive")

    assert catalog.count("nas") == 1 and catalog.count("drive") == 1
    assert find_duplicates(catalog, "nas", NASConnector(str(nas))) == []
    assert find_duplicates(catalog, "drive", NASConnector(str(drive_root))) == []


def test_fingerprint_match_is_not_enough(nas, catalog):
    """Same size, same first and last 64 KB, different middle. Must not archive."""
    decoy = bytearray(BIG)
    decoy[len(BIG) // 2] ^= 0xFF
    _write(nas, "real.jpg", BIG, mtime=1_000_000)
    _write(nas, "copy.jpg", BIG, mtime=2_000_000)
    _write(nas, "impostor.jpg", bytes(decoy), mtime=3_000_000)
    conn = _indexed(nas, catalog)

    group = find_duplicates(catalog, "nas", conn)[0]

    assert group.keeper["item_id"] == "real.jpg"
    assert [r["item_id"] for r in group.losers] == ["copy.jpg"]
    assert "differ in content" in group.confirm_note


def test_unconfirmed_groups_refuse_to_archive(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    conn = _indexed(nas, catalog)

    # No connector => nothing can be verified.
    group = find_duplicates(catalog, "nas", connector=None, confirm=True)[0]
    assert not group.confirmed

    result = ArchiveDuplicatesAction(group, conn, catalog).run(commit=True)

    assert result.status == STATUS_FAILED
    assert "not confirmed" in result.error
    assert (nas / "b.jpg").exists(), "an unverified duplicate must survive"


def test_small_files_need_no_confirmation(nas, catalog):
    """Under 128 KB the quick hash already covers every byte."""
    small = b"tiny file contents"
    _write(nas, "a.txt", small, mtime=1_000_000)
    _write(nas, "b.txt", small, mtime=2_000_000)
    conn = _indexed(nas, catalog)

    group = find_duplicates(catalog, "nas", connector=None)[0]

    assert group.confirmed
    assert "covers every byte" in group.confirm_note


def test_stale_catalog_refuses_to_act(nas, catalog):
    """If the catalog has drifted from disk, the group might be the last copy."""
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    conn = _indexed(nas, catalog)
    group = find_duplicates(catalog, "nas", conn)[0]

    (nas / "b.jpg").unlink()          # disappeared behind the catalog's back

    result = ArchiveDuplicatesAction(group, conn, catalog).run(commit=True)

    assert result.status == STATUS_FAILED
    assert "re-run the scan" in result.error


# --------------------------------------------------------------------------- #
# Archiving
# --------------------------------------------------------------------------- #
def test_archive_keeps_exactly_one_and_is_reversible(nas, catalog):
    _write(nas, "Photos/img.jpg", BIG, mtime=1_000_000)
    _write(nas, "Backup/img.jpg", BIG, mtime=2_000_000)
    _write(nas, "Old/img.jpg", BIG, mtime=3_000_000)
    conn = _indexed(nas, catalog)
    group = find_duplicates(catalog, "nas", conn)[0]

    result = ArchiveDuplicatesAction(group, conn, catalog).run(commit=True)

    assert result.status == STATUS_OK
    assert (nas / "Photos/img.jpg").exists(), "the keeper must survive"
    assert not (nas / "Backup/img.jpg").exists()
    # Recoverable, with the original structure preserved.
    assert (conn.trash / "Backup/img.jpg").exists()
    assert (conn.trash / "Old/img.jpg").exists()
    assert result.outputs["bytes_reclaimed"] == 2 * len(BIG)


def test_dry_run_archives_nothing(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    conn = _indexed(nas, catalog)
    group = find_duplicates(catalog, "nas", conn)[0]

    result = ArchiveDuplicatesAction(group, conn, catalog).run()   # no commit

    assert result.committed is False
    assert (nas / "a.jpg").exists() and (nas / "b.jpg").exists()


def test_dedup_source_action_end_to_end(nas, catalog):
    _write(nas, "Photos/one.jpg", BIG, mtime=1_000_000)
    _write(nas, "Backup/one.jpg", BIG, mtime=2_000_000)
    other = BIG[:-40] + b"x" * 40
    _write(nas, "Photos/two.jpg", other, mtime=1_000_000)
    _write(nas, "Backup/two.jpg", other, mtime=2_000_000)
    conn = _indexed(nas, catalog)

    result = DedupSourceAction("nas", conn, catalog).run(commit=True)

    assert result.status == STATUS_OK
    assert result.outputs["groups_archived"] == 2
    assert (nas / "Photos/one.jpg").exists()
    assert (nas / "Photos/two.jpg").exists()
    assert not (nas / "Backup/one.jpg").exists()
    assert not (nas / "Backup/two.jpg").exists()


def test_dedup_source_on_clean_library_is_a_noop(nas, catalog):
    _write(nas, "a.jpg", BIG)
    _write(nas, "b.jpg", b"different" * 20000)
    conn = _indexed(nas, catalog)

    result = DedupSourceAction("nas", conn, catalog).run(commit=True)

    assert result.status == STATUS_NOOP


def test_dedup_needs_an_index_first(nas, catalog):
    _write(nas, "a.jpg", BIG)
    conn = NASConnector(str(nas))          # never scanned

    result = DedupSourceAction("nas", conn, catalog).run(commit=True)

    assert result.status == STATUS_FAILED
    assert "run an index first" in result.error


# --------------------------------------------------------------------------- #
# Indexing and resume
# --------------------------------------------------------------------------- #
def test_index_records_every_file(nas, catalog):
    _write(nas, "Photos/2026-01/a.jpg", BIG)
    _write(nas, "Photos/2026-01/b.jpg", b"b" * 1000)
    _write(nas, "Docs/c.pdf", b"c" * 1000)
    conn = NASConnector(str(nas))

    result = IndexAction("nas", conn, catalog).run(commit=True)

    assert result.status == STATUS_OK
    assert result.outputs["files_indexed"] == 3
    assert catalog.count("nas") == 3


def test_interrupted_scan_resumes(nas, catalog):
    _write(nas, "A/one.jpg", b"1" * 500)
    _write(nas, "B/two.jpg", b"2" * 500)
    conn = NASConnector(str(nas))

    # Simulate a crash after the first directory.
    catalog.begin_scan("nas", resume=False)
    catalog.checkpoint("nas", "A", 1)
    assert catalog.scan_state("nas")["complete"] == 0

    report = scan(conn, catalog, source="nas")

    assert report.resumed_from == "A"
    assert catalog.scan_state("nas")["complete"] == 1


def test_wasted_bytes_counts_only_the_extras(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    _write(nas, "c.jpg", BIG, mtime=3_000_000)
    _indexed(nas, catalog)

    # Three copies of one file: two are redundant.
    assert catalog.wasted_bytes("nas") == 2 * len(BIG)
