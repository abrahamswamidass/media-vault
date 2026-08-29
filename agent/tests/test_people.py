"""
Face clustering tests — catalog/people.py's assign_person(). Pure logic
against a real (temp-file) Catalog; no model, no insightface needed here —
that's faces.py's job, tested separately in test_faces.py.
"""
from __future__ import annotations

import struct

import pytest

from mediavault.catalog import Catalog, assign_person


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
