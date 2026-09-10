"""
`unpublish` — clears published_at only, narrower than `reset` (which deletes
the catalog rows entirely). Added alongside a fix to unpublished()'s and
not_cold_archived()'s ordering: both used to sort by indexed_at, item_id,
but indexed_at is overwritten on every re-index pass (even for unchanged
files -- see scanner.py's upsert), so a scheduled re-index running between
two publish batches reshuffled the whole worklist underneath them. Sorting
by item_id alone makes it stable regardless of what else re-indexes.
"""
from __future__ import annotations

from mediavault.catalog import Catalog
from mediavault.cli import main


def _seed_item(catalog, source, item_id, *, published=True, indexed_at="2020-01-01"):
    catalog.conn.execute(
        "INSERT INTO items (source, item_id, name, quick_hash, indexed_at, published_at) "
        "VALUES (?, ?, ?, 'hash', ?, ?)",
        (source, item_id, item_id, indexed_at, "2020-01-02" if published else None),
    )
    catalog.conn.commit()


# --------------------------------------------------------------------------- #
# CLI: dry-run / --commit contract
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_change_the_catalog(tmp_path):
    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_item(catalog, "nas", "a.jpg")
        _seed_item(catalog, "nas", "b.jpg")

    assert main(["unpublish", "nas", "--db", db]) == 0

    with Catalog(db) as catalog:
        assert catalog.published_count("nas") == 2  # untouched


def test_commit_clears_published_at_for_the_given_source_only(tmp_path):
    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_item(catalog, "nas", "a.jpg")
        _seed_item(catalog, "nas", "b.jpg")
        _seed_item(catalog, "drive", "c.jpg")  # a different source -- must stay untouched

    assert main(["unpublish", "nas", "--db", db, "--commit"]) == 0

    with Catalog(db) as catalog:
        assert catalog.published_count("nas") == 0
        assert catalog.published_count("drive") == 1
        # Nothing else about the row changes -- only published_at.
        row = catalog.conn.execute(
            "SELECT quick_hash, published_at FROM items WHERE item_id = 'a.jpg'").fetchone()
        assert row["quick_hash"] == "hash"
        assert row["published_at"] is None


def test_commit_makes_items_eligible_for_a_plain_republish(tmp_path):
    """The actual point of the command: after clearing published_at, a plain
    publish (no --force) picks these items up again on its own."""
    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_item(catalog, "nas", "a.jpg")
        assert len(catalog.unpublished("nas")) == 0  # already published, excluded

    assert main(["unpublish", "nas", "--db", db, "--commit"]) == 0

    with Catalog(db) as catalog:
        pending = catalog.unpublished("nas")
        assert [r["item_id"] for r in pending] == ["a.jpg"]


def test_already_unpublished_items_are_unaffected(tmp_path):
    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_item(catalog, "nas", "a.jpg", published=False)

    assert main(["unpublish", "nas", "--db", db, "--commit"]) == 0

    with Catalog(db) as catalog:
        assert catalog.published_count("nas") == 0  # was already 0, stays 0


# --------------------------------------------------------------------------- #
# Store: ordering stability, item_id not indexed_at
# --------------------------------------------------------------------------- #
def test_unpublished_order_is_stable_across_a_reindex(tmp_path):
    """Regression: indexed_at used to be the primary sort key, but it's
    overwritten on every re-index pass -- even for unchanged files. A
    scheduled weekly re-index running mid-campaign used to reorder the
    entire remaining worklist. Sorting by item_id alone means a "re-index"
    (simulated here by bumping indexed_at on an already-seen row) can't
    change the order at all."""
    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_item(catalog, "nas", "c.jpg", published=False, indexed_at="2020-01-01")
        _seed_item(catalog, "nas", "a.jpg", published=False, indexed_at="2020-01-02")
        _seed_item(catalog, "nas", "b.jpg", published=False, indexed_at="2020-01-03")

        before = [r["item_id"] for r in catalog.unpublished("nas")]
        assert before == ["a.jpg", "b.jpg", "c.jpg"]  # item_id order, not insert/indexed_at order

        # Simulate a re-index touching every row's indexed_at (scanner.py's
        # upsert does this even when nothing about the file actually changed).
        catalog.conn.execute(
            "UPDATE items SET indexed_at = '2099-01-01' WHERE item_id = 'c.jpg'")
        catalog.conn.commit()

        after = [r["item_id"] for r in catalog.unpublished("nas")]
        assert after == before  # unchanged -- item_id sort doesn't care about indexed_at


def test_not_cold_archived_order_is_also_stable(tmp_path):
    db = str(tmp_path / "cat.sqlite")
    with Catalog(db) as catalog:
        _seed_item(catalog, "nas", "c.jpg", indexed_at="2020-01-01")
        _seed_item(catalog, "nas", "a.jpg", indexed_at="2020-01-02")
        _seed_item(catalog, "nas", "b.jpg", indexed_at="2020-01-03")

        rows = [r["item_id"] for r in catalog.not_cold_archived("nas")]
        assert rows == ["a.jpg", "b.jpg", "c.jpg"]
