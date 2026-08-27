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
from mediavault.catalog import Catalog, find_duplicates, folder_breakdown, scan, summarize
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


def test_operational_folders_are_never_indexed(nas, catalog):
    """Trash and Amazon staging must stay out of scope once root covers the
    whole share — otherwise a full-drive scan would treat staged-for-Amazon
    copies as duplicates of the originals and archive them out from under the
    Amazon desktop app before it uploads them."""
    _write(nas, "Photos/img.jpg", BIG, mtime=1_000_000)
    _write(nas, "_trash/old_img.jpg", BIG, mtime=500_000)
    _write(nas, "_AmazonUpload/2026-01/img.jpg", BIG, mtime=500_000)

    conn = NASConnector(str(nas), exclude=[str(nas / "_AmazonUpload")])
    scan(conn, catalog, source="nas")

    ids = {row["item_id"] for row in catalog.conn.execute(
        "SELECT item_id FROM items WHERE source = 'nas'")}
    assert ids == {"Photos/img.jpg"}

    # And dedup never sees the staged copy as a "duplicate" to archive.
    groups = find_duplicates(catalog, "nas", conn)
    assert groups == []


# --------------------------------------------------------------------------- #
# Catalog reset (testing convenience)
# --------------------------------------------------------------------------- #
def test_reset_wipes_one_source_only(nas, tmp_path, catalog):
    _write(nas, "Photos/img.jpg", BIG)
    drive_root = tmp_path / "drive"
    _write(drive_root, "img.jpg", BIG)
    scan(NASConnector(str(nas)), catalog, source="nas")
    scan(NASConnector(str(drive_root)), catalog, source="drive")

    result = catalog.reset("nas")

    assert result == {"items_deleted": 1, "scans_deleted": 1}
    assert catalog.count("nas", state="active") == 0
    assert catalog.count("drive", state="active") == 1  # untouched


def test_reset_all_wipes_every_source(nas, tmp_path, catalog):
    _write(nas, "Photos/img.jpg", BIG)
    drive_root = tmp_path / "drive"
    _write(drive_root, "img.jpg", BIG)
    scan(NASConnector(str(nas)), catalog, source="nas")
    scan(NASConnector(str(drive_root)), catalog, source="drive")

    result = catalog.reset(None)

    assert result == {"items_deleted": 2, "scans_deleted": 2}
    assert catalog.sources() == []


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


def test_resume_skip_phase_reports_on_list_for_every_directory(nas, catalog):
    """The skip phase (fast-forwarding to the cursor) re-walks every prior
    directory with otherwise zero progress output — on_list is what makes a
    hang during it distinguishable from 'just resumed, nothing yet' (#11)."""
    _write(nas, "A/one.jpg", b"1" * 500)
    _write(nas, "B/two.jpg", b"2" * 500)
    _write(nas, "C/three.jpg", b"3" * 500)
    conn = NASConnector(str(nas))

    catalog.begin_scan("nas", resume=False)
    catalog.checkpoint("nas", "C", 0)  # pretend A and B are already done

    listed = []
    scan(conn, catalog, source="nas", on_list=listed.append)

    # Every directory up to and including the cursor gets listed during the
    # skip phase, not just the resume target.
    assert listed[:4] == ["", "A", "B", "C"]


def test_wasted_bytes_counts_only_the_extras(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    _write(nas, "c.jpg", BIG, mtime=3_000_000)
    _indexed(nas, catalog)

    # Three copies of one file: two are redundant.
    assert catalog.wasted_bytes("nas") == 2 * len(BIG)


# --------------------------------------------------------------------------- #
# folder_breakdown — summarizing a large dedup run by location
# --------------------------------------------------------------------------- #
def test_folder_breakdown_buckets_by_path_depth(nas, catalog):
    # Two redundant copies in 2019, one in 2020 — same total group structure
    # (the 2019 batch keeps its oldest, the 2020 one keeps its only original).
    _write(nas, "Photos/2019/a.jpg", BIG, mtime=1_000_000)
    _write(nas, "Photos/2019/a_copy.jpg", BIG, mtime=2_000_000)
    _write(nas, "Photos/2019/a_copy2.jpg", BIG, mtime=3_000_000)
    _write(nas, "Photos/2020/b.jpg", TAIL, mtime=1_000_000)
    _write(nas, "Photos/2020/b_copy.jpg", TAIL, mtime=2_000_000)
    conn = _indexed(nas, catalog)

    groups = find_duplicates(catalog, "nas", conn)
    breakdown = folder_breakdown(groups, depth=2)

    by_folder = {b["folder"]: b for b in breakdown}
    assert by_folder["Photos/2019"]["copies"] == 2       # the two redundant 2019 copies
    assert by_folder["Photos/2020"]["copies"] == 1       # the one redundant 2020 copy
    # Sorted by reclaimable_bytes descending — 2019 has more redundant bytes.
    assert breakdown[0]["folder"] == "Photos/2019"


def test_folder_breakdown_only_counts_archivable_groups(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", b"unique content, no duplicate" * 100)
    conn = _indexed(nas, catalog)

    groups = find_duplicates(catalog, "nas", conn)
    assert folder_breakdown(groups) == []


# --------------------------------------------------------------------------- #
# Confirmation survives a connector's own non-OSError transient failures
# --------------------------------------------------------------------------- #
class _TransientConnectorError(Exception):
    """Stand-in for a connector's own transient-connection exception type
    that isn't an OSError subclass — smbprotocol's SMBConnectionClosed and
    SMBAuthenticationError are exactly this in practice."""


class _FlakyReadConnector:
    """Wraps a real connector but makes read() raise a non-OSError exception
    for one item_id, simulating a connection drop that outlasted the
    connector's own retry/reconnect attempts."""

    def __init__(self, inner, fail_item_id):
        self._inner = inner
        self._fail_item_id = fail_item_id
        self.name = inner.name

    def read(self, item_id, nbytes=0):
        if item_id == self._fail_item_id:
            raise _TransientConnectorError("connection dropped")
        return self._inner.read(item_id, nbytes)

    def stat(self, item_id):
        return self._inner.stat(item_id)

    def list(self, prefix="", limit=100):
        return self._inner.list(prefix, limit)


def test_confirm_survives_a_non_oserror_read_failure(nas, catalog):
    """Regression: a connector's own transient exception type (not an
    OSError subclass) used to propagate straight out of _confirm(), crashing
    the entire dedup run instead of just marking that one group unconfirmed
    — a real risk given confirmation full-hashes every candidate over 128KB,
    potentially thousands of them, over the same connection that can drop."""
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    real_conn = _indexed(nas, catalog)
    flaky = _FlakyReadConnector(real_conn, fail_item_id="a.jpg")  # the keeper

    groups = find_duplicates(catalog, "nas", flaky)  # must not raise

    assert len(groups) == 1
    assert groups[0].confirmed is False
    assert "could not read keeper" in groups[0].confirm_note
    assert not groups[0].safe_to_archive


# --------------------------------------------------------------------------- #
# on_confirm progress — only fires for groups that actually need a real read
# --------------------------------------------------------------------------- #
def test_on_confirm_fires_only_for_groups_needing_a_real_read(nas, catalog):
    _write(nas, "big_a.jpg", BIG, mtime=1_000_000)      # > quick-hash window
    _write(nas, "big_b.jpg", BIG, mtime=2_000_000)
    _write(nas, "small_a.jpg", b"tiny", mtime=1_000_000)  # <= window, free confirm
    _write(nas, "small_b.jpg", b"tiny", mtime=2_000_000)
    conn = _indexed(nas, catalog)

    calls = []
    groups = find_duplicates(catalog, "nas", conn, on_confirm=lambda d, t, i: calls.append((d, t, i)))

    assert len(groups) == 2
    # Only the BIG group needed a real read; the tiny one is free.
    assert calls == [(1, 1, "big_a.jpg")]


def test_on_confirm_reports_an_accurate_total_upfront(nas, catalog):
    # Each group's content must itself exceed the 128KB quick-hash coverage
    # window (BIG does — a distinct one-byte prefix per group is enough to
    # give each its own quick_hash while staying well over that size).
    for i in range(3):
        content = bytes([i]) + BIG
        _write(nas, f"g{i}_a.jpg", content, mtime=1_000_000)
        _write(nas, f"g{i}_b.jpg", content, mtime=2_000_000)
    conn = _indexed(nas, catalog)

    calls = []
    find_duplicates(catalog, "nas", conn, on_confirm=lambda d, t, i: calls.append((d, t)))

    assert [t for _d, t in calls] == [3, 3, 3]
    assert [d for d, _t in calls] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# full_hash caching — a confirmed hash survives into the catalog and is
# reused by a later run instead of triggering another full-content read.
# --------------------------------------------------------------------------- #
def test_confirmation_persists_full_hash_to_the_catalog(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    conn = _indexed(nas, catalog)

    find_duplicates(catalog, "nas", conn)

    row = catalog.conn.execute(
        "SELECT full_hash FROM items WHERE item_id = 'a.jpg'").fetchone()
    assert row["full_hash"]  # a real hex digest, not NULL


def test_a_second_run_reuses_the_cached_hash_without_reading_again(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    conn = _indexed(nas, catalog)
    find_duplicates(catalog, "nas", conn)  # first run: populates full_hash

    reads = []
    real_read = conn.read
    conn.read = lambda item_id, nbytes=0: (reads.append(item_id), real_read(item_id, nbytes))[1]

    groups = find_duplicates(catalog, "nas", conn)  # second run: should hit cache

    assert reads == []
    assert groups[0].confirmed and groups[0].losers


def test_each_cached_hash_commits_immediately_not_at_the_end_of_the_run(nas, catalog):
    """Regression: set_full_hash() used to write without committing, so the
    whole confirmation pass ran inside one long-lived open transaction —
    both a crash-safety bug (an interruption rolled back every hash
    confirmed so far, the exact thing caching was supposed to prevent) and a
    concurrency bug (a second writer, e.g. `publish` in another terminal,
    blocked past its busy_timeout with "database is locked")."""
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    conn = _indexed(nas, catalog)

    find_duplicates(catalog, "nas", conn)

    assert not catalog.conn.in_transaction


def test_reindexing_a_changed_file_invalidates_its_cached_hash(nas, catalog):
    _write(nas, "a.jpg", BIG, mtime=1_000_000)
    _write(nas, "b.jpg", BIG, mtime=2_000_000)
    conn = _indexed(nas, catalog)
    find_duplicates(catalog, "nas", conn)  # populates full_hash for both

    _write(nas, "a.jpg", BIG + b"more", mtime=1_000_000)
    scan(conn, catalog, source="nas")  # re-index: quick_hash changes for a.jpg

    row = catalog.conn.execute(
        "SELECT full_hash FROM items WHERE item_id = 'a.jpg'").fetchone()
    assert row["full_hash"] is None  # stale hash cleared, not silently reused
