"""
match_report is deliberately separate from dedup.py: dedup never compares
across sources (see dedup.py's module docstring and duplicate_groups' own
"do not relax" comment), but match_report exists specifically to do that,
read-only, for a person deciding what to prune from Drive by hand.
"""
from __future__ import annotations

from mediavault.catalog.store import Catalog
from mediavault.catalog import match_report


def _seed(catalog, source, item_id, name, size, quick_hash, state="active"):
    catalog.conn.execute(
        "INSERT INTO items (source, item_id, name, size, quick_hash, indexed_at, state) "
        "VALUES (?, ?, ?, ?, ?, 'now', ?)",
        (source, item_id, name, size, quick_hash, state),
    )
    catalog.conn.commit()


def test_finds_a_file_present_on_both_sources_by_quick_hash(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "nas", "Photos/2020/a.jpg", "a.jpg", 100, "100:abc")
        _seed(catalog, "drive", "driveid123", "a.jpg", 100, "100:abc")

        matches = match_report.find_matches(catalog, "nas", "drive")

        assert len(matches) == 1
        assert matches[0]["a_path"] == "Photos/2020/a.jpg"
        assert matches[0]["b_id"] == "driveid123"


def test_a_file_only_on_nas_is_not_a_match(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "nas", "Photos/only_nas.jpg", "only_nas.jpg", 100, "100:xyz")

        assert match_report.find_matches(catalog, "nas", "drive") == []


def test_archived_nas_item_is_excluded_even_if_drive_still_has_it(tmp_path):
    """Regression: an item soft-deleted on nas shouldn't still show up as
    'matched' just because the drive copy is untouched."""
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "nas", "Photos/gone.jpg", "gone.jpg", 100, "100:abc", state="archived")
        _seed(catalog, "drive", "driveid1", "gone.jpg", 100, "100:abc")

        assert match_report.find_matches(catalog, "nas", "drive") == []


def test_build_tree_nests_by_path_and_rolls_up_folder_sizes(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "nas", "Photos/2020/a.jpg", "a.jpg", 100, "100:aaa")
        _seed(catalog, "drive", "d1", "a.jpg", 100, "100:aaa")
        _seed(catalog, "nas", "Photos/2020/b.jpg", "b.jpg", 200, "200:bbb")
        _seed(catalog, "drive", "d2", "b (1).jpg", 200, "200:bbb")

        matches = match_report.find_matches(catalog, "nas", "drive")
        tree = match_report.build_tree(matches)

        photos = tree.children["Photos"]
        year = photos.children["2020"]
        assert year.size == 300  # rolled up from both matched files
        assert photos.size == 300
        b_leaf = year.children["b.jpg"]
        assert b_leaf.match["b_name"] == "b (1).jpg"
        assert b_leaf.match["b_size"] == 200


def test_render_tree_sorts_largest_first_at_each_level(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "nas", "small.jpg", "small.jpg", 10, "10:a")
        _seed(catalog, "drive", "d1", "small.jpg", 10, "10:a")
        _seed(catalog, "nas", "big.jpg", "big.jpg", 999, "999:b")
        _seed(catalog, "drive", "d2", "big.jpg", 999, "999:b")

        matches = match_report.find_matches(catalog, "nas", "drive")
        text = match_report.render_tree(match_report.build_tree(matches))

        assert text.index("big.jpg") < text.index("small.jpg")


def test_min_size_skips_small_files(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "nas", "tiny.jpg", "tiny.jpg", 5, "5:a")
        _seed(catalog, "drive", "d1", "tiny.jpg", 5, "5:a")

        assert match_report.find_matches(catalog, "nas", "drive", min_size=100) == []
