"""
CLI-level tests for `people-recluster` — verified through the actual entry
point (main()), matching test_cli_cold_archive.py's precedent for commands
whose dry-run/--commit contract matters at this layer, not just the
underlying catalog/people.py logic (already covered in test_people.py).
"""
from __future__ import annotations

from mediavault.catalog import Catalog, assign_person
import struct


def _embedding(*values: float) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


def _seed_split_person(catalog):
    """Same drift-chain scenario as test_people.py's regression test: three
    faces of one real person, split into two by the greedy algorithm."""
    p1 = assign_person(catalog, _embedding(0.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(0.0, 0.0, 0.0), p1)
    p2 = assign_person(catalog, _embedding(0.5, 0.0, 0.0))
    catalog.add_face("nas", "b.jpg", (0, 0, 10, 10), 0.9, _embedding(0.5, 0.0, 0.0), p2)
    p3 = assign_person(catalog, _embedding(1.0, 0.0, 0.0))
    catalog.add_face("nas", "c.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), p3)


def test_no_faces_is_a_clean_noop(tmp_path):
    from mediavault.cli import main

    db = str(tmp_path / "cat.sqlite")
    with Catalog(db):
        pass

    assert main(["people-recluster", "--db", db]) == 0


def test_dry_run_does_not_change_the_catalog(tmp_path):
    from mediavault.cli import main

    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_split_person(catalog)
        before = catalog.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]

    assert main(["people-recluster", "--db", db]) == 0

    with Catalog(db) as catalog:
        after = catalog.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert after == before == 2  # the drift chain splits into two people, not three


def test_commit_merges_the_split_person(tmp_path):
    from mediavault.cli import main

    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_split_person(catalog)

    assert main(["people-recluster", "--db", db, "--commit"]) == 0

    with Catalog(db) as catalog:
        assert catalog.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1
        ids = {row["person_id"] for row in catalog.all_faces_with_embeddings()}
        assert len(ids) == 1


def test_threshold_flag_is_respected(tmp_path):
    """A threshold tight enough that even the immediate-neighbor distance
    (0.5) doesn't qualify should keep every face as its own person."""
    from mediavault.cli import main

    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_split_person(catalog)

    assert main(["people-recluster", "--db", db, "--threshold", "0.1", "--commit"]) == 0

    with Catalog(db) as catalog:
        assert catalog.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3
