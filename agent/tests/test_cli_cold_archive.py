"""
CLI-level tests for `cold-archive` — pushing NAS originals to a separate
cold-storage bucket for off-site backup, meant to run incrementally (e.g.
weekly) rather than once. Verified through the actual CLI entry point
(main()), matching test_cli_dedup.py's precedent for flags whose contract
matters at the command layer, not just the Action layer.
"""
from __future__ import annotations

from mediavault.actions.coldstorage import cold_key
from mediavault.catalog import Catalog
from mediavault.cli import main


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _setup(tmp_path, n=3):
    nas = tmp_path / "nas"
    nas.mkdir()
    for i in range(n):
        _write(nas, f"img_{i}.jpg", f"photo{i}".encode() * 1000)
    db = str(tmp_path / "cat.sqlite")
    assert main(["index", "nas", "--root", str(nas), "--db", db, "--quiet"]) == 0
    return nas, db


def test_dry_run_uploads_nothing(tmp_path):
    nas, db = _setup(tmp_path)

    assert main(["cold-archive", "nas", "--root", str(nas), "--db", db,
                "--coldstore-dir", str(tmp_path / "cold"),
                "--log-dir", str(tmp_path / "actions")]) == 0

    with Catalog(db) as catalog:
        assert catalog.cold_archived_count("nas") == 0


def test_max_items_caps_how_many_files_get_pushed(tmp_path):
    """The exact feature this was built for: a small test batch before
    committing to the full library."""
    nas, db = _setup(tmp_path, n=5)

    assert main(["cold-archive", "nas", "--root", str(nas), "--db", db,
                "--coldstore-dir", str(tmp_path / "cold"),
                "--log-dir", str(tmp_path / "actions"),
                "--max-items", "2", "--commit"]) == 0

    with Catalog(db) as catalog:
        assert catalog.cold_archived_count("nas") == 2


def test_rerun_only_pushes_whats_new(tmp_path):
    """The weekly-run shape: a second pass with a higher/no cap must only
    touch what the first pass left behind, not re-upload everything."""
    nas, db = _setup(tmp_path, n=5)
    cold_dir = str(tmp_path / "cold")

    main(["cold-archive", "nas", "--root", str(nas), "--db", db,
         "--coldstore-dir", cold_dir, "--log-dir", str(tmp_path / "actions"),
         "--max-items", "2", "--commit"])
    # A new file shows up between runs, the way a weekly cadence would see it.
    _write(nas, "img_new.jpg", b"brand-new" * 1000)
    main(["index", "nas", "--root", str(nas), "--db", db, "--quiet"])

    main(["cold-archive", "nas", "--root", str(nas), "--db", db,
         "--coldstore-dir", cold_dir, "--log-dir", str(tmp_path / "actions"),
         "--commit"])

    with Catalog(db) as catalog:
        # 5 originals + 1 new one, all six now pushed.
        assert catalog.cold_archived_count("nas") == 6


def test_uploaded_object_is_keyed_by_relative_path(tmp_path):
    from mediavault.blobstore import LocalBlobStore

    nas, db = _setup(tmp_path, n=1)
    cold_dir = str(tmp_path / "cold")

    main(["cold-archive", "nas", "--root", str(nas), "--db", db,
         "--coldstore-dir", cold_dir, "--log-dir", str(tmp_path / "actions"),
         "--commit"])

    assert LocalBlobStore(cold_dir).exists(cold_key("img_0.jpg"))
