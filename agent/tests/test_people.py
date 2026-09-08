"""
Face clustering tests — catalog/people.py's assign_person(). Pure logic
against a real (temp-file) Catalog; no model, no insightface needed here —
that's faces.py's job, tested separately in test_faces.py.
"""
from __future__ import annotations

import struct

import pytest

from mediavault.catalog import Catalog, assign_person, recluster


def _embedding(*values: float) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


@pytest.fixture
def catalog(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as c:
        yield c


def test_first_face_ever_creates_a_new_person(catalog):
    person_id = assign_person(catalog, _embedding(1.0, 0.0, 0.0))

    assert person_id is not None
    people = catalog.list_people()
    assert people == []  # not a real person until a face row references them


def test_a_close_embedding_matches_the_existing_person(catalog):
    first = assign_person(catalog, _embedding(1.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), first)

    second = assign_person(catalog, _embedding(1.01, 0.0, 0.0))  # tiny difference

    assert second == first


def test_a_far_embedding_creates_a_new_person(catalog):
    first = assign_person(catalog, _embedding(1.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), first)

    second = assign_person(catalog, _embedding(-1.0, 0.0, 0.0))  # opposite vector

    assert second != first


def test_reset_people_clears_faces_and_people_but_nothing_else(catalog):
    """Recovery path for a bad clustering run (e.g. the embedding bug fixed
    in faces.py) — must wipe faces/people without touching items/scans, so
    published items don't need a full reset + re-index to try again."""
    catalog.conn.execute(
        "INSERT INTO items (source, item_id, name, indexed_at, published_at) "
        "VALUES ('nas', 'a.jpg', 'a.jpg', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
    )
    catalog.conn.commit()
    p1 = assign_person(catalog, _embedding(1.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), p1)

    result = catalog.reset_people()

    assert result == {"faces_deleted": 1, "people_deleted": 1}
    assert catalog.list_people() == []
    assert catalog.faces_for_item("nas", "a.jpg") == []
    row = catalog.get("nas", "a.jpg")
    assert row is not None and row["published_at"] is not None  # untouched


def test_on_match_reports_the_nearest_distance_and_outcome(catalog):
    """publish --debug uses this to show real distance numbers — the only
    way to actually see why two photos did or didn't cluster together."""
    calls = []
    on_match = lambda best_id, best_dist, matched: calls.append((best_id, best_dist, matched))

    first = assign_person(catalog, _embedding(1.0, 0.0, 0.0), on_match=on_match)
    assert calls[-1] == (None, None, False)  # nothing to compare against yet

    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), first)
    assign_person(catalog, _embedding(1.0, 0.0, 0.0), on_match=on_match)
    assert calls[-1] == (first, pytest.approx(0.0), True)

    assign_person(catalog, _embedding(-1.0, 0.0, 0.0), on_match=on_match)
    best_id, best_dist, matched = calls[-1]
    assert best_id == first
    assert best_dist == pytest.approx(2.0)  # opposite unit vectors
    assert matched is False


def test_matching_uses_each_persons_first_face_not_the_most_recent(catalog):
    """Centroid = first-ever face, deliberately not recomputed as more faces
    are added — a later, slightly different face shouldn't change who a new
    face gets compared against."""
    p1 = assign_person(catalog, _embedding(1.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), p1)
    # A second, slightly different face for the same person — shouldn't
    # become the new comparison point.
    catalog.add_face("nas", "b.jpg", (0, 0, 10, 10), 0.9, _embedding(0.5, 0.5, 0.0), p1)

    matched = assign_person(catalog, _embedding(1.0, 0.0, 0.0))

    assert matched == p1


# --------------------------------------------------------------------------- #
# recluster() — fixes the failure mode the test above deliberately locks in:
# assign_person only ever compares against a cluster's first-ever face, so a
# face that's drifted past MATCH_THRESHOLD from that one anchor (but is still
# close to some OTHER member already in the cluster) wrongly starts a new
# person instead of joining. recluster compares against every member.
# --------------------------------------------------------------------------- #
def test_recluster_merges_a_chain_the_greedy_algorithm_would_split(catalog):
    """The exact bug this exists to fix: three faces of the same person in
    a drift chain -- each close to its immediate neighbor, but the third
    is too far from the first for assign_person's centroid-only check."""
    threshold = 0.9
    p1 = assign_person(catalog, _embedding(0.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(0.0, 0.0, 0.0), p1)
    p2 = assign_person(catalog, _embedding(0.5, 0.0, 0.0))  # dist to p1 = 0.5, matches
    assert p2 == p1
    catalog.add_face("nas", "b.jpg", (0, 0, 10, 10), 0.9, _embedding(0.5, 0.0, 0.0), p2)
    p3 = assign_person(catalog, _embedding(1.0, 0.0, 0.0))  # dist to p1's anchor = 1.0
    assert p3 != p1  # confirms the bug reproduces under the old algorithm first
    catalog.add_face("nas", "c.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), p3)

    result = recluster(catalog, threshold=threshold)

    assert result["face_count"] == 3
    assert result["cluster_count"] == 1
    faces = catalog.all_faces_with_embeddings()
    assert len({result["assignments"][f["id"]] for f in faces}) == 1


def test_recluster_keeps_genuinely_different_people_separate(catalog):
    p1 = assign_person(catalog, _embedding(1.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(1.0, 0.0, 0.0), p1)
    p2 = assign_person(catalog, _embedding(-1.0, 0.0, 0.0))
    catalog.add_face("nas", "b.jpg", (0, 0, 10, 10), 0.9, _embedding(-1.0, 0.0, 0.0), p2)

    result = recluster(catalog, threshold=0.9)

    assert result["cluster_count"] == 2


def test_recluster_is_deterministic(catalog):
    for i, val in enumerate([0.0, 0.4, 1.3, -1.0]):
        pid = assign_person(catalog, _embedding(val, 0.0, 0.0))
        catalog.add_face("nas", f"{i}.jpg", (0, 0, 10, 10), 0.9, _embedding(val, 0.0, 0.0), pid)

    first = recluster(catalog, threshold=0.9)
    second = recluster(catalog, threshold=0.9)

    assert first["cluster_count"] == second["cluster_count"]
    assert first["assignments"] == second["assignments"]


def test_recluster_reports_progress(catalog):
    for i, val in enumerate([0.0, 5.0]):
        pid = assign_person(catalog, _embedding(val, 0.0, 0.0))
        catalog.add_face("nas", f"{i}.jpg", (0, 0, 10, 10), 0.9, _embedding(val, 0.0, 0.0), pid)
    calls = []

    recluster(catalog, threshold=0.9, on_progress=lambda done, total: calls.append((done, total)))

    assert calls == [(1, 2), (2, 2)]


def test_apply_recluster_reassigns_person_id_and_drops_old_names(catalog):
    p1 = assign_person(catalog, _embedding(0.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (0, 0, 10, 10), 0.9, _embedding(0.0, 0.0, 0.0), p1)
    catalog.set_person_name(p1, "Mom")
    p2 = assign_person(catalog, _embedding(-5.0, 0.0, 0.0))
    catalog.add_face("nas", "b.jpg", (0, 0, 10, 10), 0.9, _embedding(-5.0, 0.0, 0.0), p2)

    result = recluster(catalog, threshold=0.9)
    created = catalog.apply_recluster(result["assignments"])

    assert created == 2
    people = catalog.list_people()
    assert len(people) == 2
    assert all(p["name"] is None for p in people)  # names lost, same trade-off as people-reset
    row_a = catalog.faces_for_item("nas", "a.jpg")[0]
    row_b = catalog.faces_for_item("nas", "b.jpg")[0]
    assert row_a["person_id"] != row_b["person_id"]
    assert row_a["person_id"] is not None and row_b["person_id"] is not None


def test_apply_recluster_leaves_embeddings_and_bboxes_untouched(catalog):
    p1 = assign_person(catalog, _embedding(0.0, 0.0, 0.0))
    catalog.add_face("nas", "a.jpg", (1.0, 2.0, 3.0, 4.0), 0.75,
                     _embedding(0.0, 0.0, 0.0), p1)

    result = recluster(catalog, threshold=0.9)
    catalog.apply_recluster(result["assignments"])

    row = catalog.faces_for_item("nas", "a.jpg")[0]
    assert (row["bbox_x1"], row["bbox_y1"], row["bbox_x2"], row["bbox_y2"]) == (1.0, 2.0, 3.0, 4.0)
    assert row["score"] == 0.75
    assert row["embedding"] == _embedding(0.0, 0.0, 0.0)
