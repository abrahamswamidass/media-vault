"""
Publish action tests — thumbnails + metadata facts for the (not-yet-built) web
module. Runs with no cloud account: LocalBlobStore/LocalFactsStore stand in for
GCS/Firestore, exactly like LocalBlobStore already stands in for GCS elsewhere.
"""
from __future__ import annotations

import io
import sys
import types

import pytest

PIL = pytest.importorskip("PIL", reason="thumbnailing needs Pillow (imaging extra)")
from PIL import Image  # noqa: E402

from mediavault import metadata
from mediavault.actions import STATUS_FAILED, STATUS_NOOP, STATUS_OK
from mediavault.actions.maintenance import PublishAction
from mediavault.blobstore import LocalBlobStore, blob_key
from mediavault.catalog import Catalog, scan
from mediavault.connectors.nas import NASConnector
from mediavault.sync.facts import LocalFactsStore


def _jpeg_bytes(color=(120, 180, 90), size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def nas(tmp_path):
    root = tmp_path / "nas"
    (root / "Photos").mkdir(parents=True)
    (root / "Photos" / "real.jpg").write_bytes(_jpeg_bytes())
    return root


@pytest.fixture
def catalog(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as c:
        yield c


@pytest.fixture
def blobs(tmp_path):
    return LocalBlobStore(str(tmp_path / "blobs"))


@pytest.fixture
def facts(tmp_path):
    return LocalFactsStore(str(tmp_path / "facts"))


def _indexed(root, catalog, source="nas"):
    conn = NASConnector(str(root))
    scan(conn, catalog, source=source)
    return conn


def test_dry_run_publishes_nothing(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=False)

    assert result.status == STATUS_OK
    assert not result.committed
    assert catalog.published_count("nas") == 0
    assert not (blobs.root / "thumbs").exists()


def test_commit_pushes_thumbnail_and_fact(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    row = catalog.get("nas", "Photos/real.jpg")

    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    assert result.status == STATUS_OK
    assert result.outputs["published"] == 1
    assert catalog.published_count("nas") == 1

    key = blob_key(row["quick_hash"], "thumbs", "webp")
    assert blobs.exists(key)

    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert fact_file.exists()
    assert row["quick_hash"] in fact_file.read_text()


@pytest.fixture
def fake_exiftool(monkeypatch):
    """Same fake-module injection as test_metadata.py — no real exiftool
    binary needed to verify PublishAction wires the result through."""
    state = {"result": []}

    class FakeExifToolHelper:
        def get_tags(self, files, tags, params=None):
            return state["result"]

    monkeypatch.setitem(sys.modules, "exiftool",
                        types.SimpleNamespace(ExifToolHelper=FakeExifToolHelper))
    monkeypatch.setattr(metadata, "_helper", None)
    return state


def test_commit_extracts_and_stores_exif(nas, catalog, blobs, facts, fake_exiftool):
    fake_exiftool["result"] = [{
        "File:ImageWidth": 800, "File:ImageHeight": 600,
        "EXIF:DateTimeOriginal": "2026:01:15 10:30:00",
        "EXIF:Make": "Canon", "EXIF:Model": "EOS R5",
    }]
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    row = catalog.get("nas", "Photos/real.jpg")
    assert row["width"] == 800
    assert row["camera_model"] == "EOS R5"
    assert row["date_taken"] is not None

    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert "EOS R5" in fact_file.read_text()


def test_commit_extracts_and_stores_gps(nas, catalog, blobs, facts, fake_exiftool):
    fake_exiftool["result"] = [{
        "Composite:GPSLatitude": 37.7749, "Composite:GPSLongitude": -122.4194,
    }]
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    row = catalog.get("nas", "Photos/real.jpg")
    assert row["latitude"] == 37.7749
    assert row["longitude"] == -122.4194

    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert "37.7749" in fact_file.read_text()


def test_missing_gps_leaves_coordinates_null_not_zero(nas, catalog, blobs, facts, fake_exiftool):
    """0,0 is a real place (off the coast of West Africa) — an absent GPS
    block must stay NULL, never silently become that valid-looking pair."""
    fake_exiftool["result"] = [{"EXIF:Make": "Canon"}]
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    row = catalog.get("nas", "Photos/real.jpg")
    assert row["latitude"] is None
    assert row["longitude"] is None


def test_commit_extracts_and_stores_video_duration(nas, catalog, blobs, facts, fake_exiftool):
    fake_exiftool["result"] = [{"Composite:Duration": 12.34}]
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    row = catalog.get("nas", "Photos/real.jpg")
    assert row["duration_seconds"] == 12.34

    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert "12.34" in fact_file.read_text()


def test_missing_exif_tool_does_not_block_publish(nas, catalog, blobs, facts, monkeypatch):
    """PyExifTool not being installed must degrade gracefully, not fail the item."""
    monkeypatch.setitem(sys.modules, "exiftool", None)
    monkeypatch.setattr(metadata, "_helper", None)
    conn = _indexed(nas, catalog)

    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    assert result.status == STATUS_OK
    assert result.outputs["published"] == 1
    row = catalog.get("nas", "Photos/real.jpg")
    assert row["width"] is None


def test_rerun_is_a_noop(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    assert result.status == STATUS_NOOP


def test_force_republishes_an_already_published_item(nas, catalog, blobs, facts, fake_exiftool):
    """Backfilling a fact field (e.g. GPS) added after first publish shouldn't
    need a full reset + re-index — force re-processes already-published rows."""
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    fake_exiftool["result"] = [{"Composite:GPSLatitude": 1.5, "Composite:GPSLongitude": 2.5}]
    result = PublishAction("nas", conn, catalog, blobs, facts, force=True).run(commit=True)

    assert result.status == STATUS_OK
    assert result.outputs["published"] == 1
    row = catalog.get("nas", "Photos/real.jpg")
    assert row["latitude"] == 1.5


def test_force_does_not_regenerate_an_existing_thumbnail(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    key = blob_key(catalog.get("nas", "Photos/real.jpg")["quick_hash"], "thumbs", "webp")
    written_at = (blobs.root / key).stat().st_mtime

    PublishAction("nas", conn, catalog, blobs, facts, force=True).run(commit=True)

    assert (blobs.root / key).stat().st_mtime == written_at


def test_without_force_rerun_still_ignores_already_published_items(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    result = PublishAction("nas", conn, catalog, blobs, facts, force=False).run(commit=True)

    assert result.status == STATUS_NOOP


def test_mime_only_skips_items_with_no_mime(nas, catalog, blobs, facts):
    """Simulates a partially re-indexed library: some rows still have no
    mime (indexed before mime detection was added), others already do —
    --mime-only must target only the ones a fresh index pass has reached."""
    (nas / "Photos" / "no_mime.jpg").write_bytes(_jpeg_bytes(color=(9, 9, 9)))
    conn = _indexed(nas, catalog)
    catalog.conn.execute(
        "UPDATE items SET mime = NULL WHERE source = 'nas' AND item_id = 'Photos/no_mime.jpg'"
    )
    catalog.conn.commit()

    result = PublishAction("nas", conn, catalog, blobs, facts, mime_only=True).run(commit=True)

    assert result.status == STATUS_OK
    assert result.outputs["published"] == 1
    assert catalog.get("nas", "Photos/real.jpg")["published_at"] is not None
    assert catalog.get("nas", "Photos/no_mime.jpg")["published_at"] is None


def test_thumbnail_key_is_content_addressed_not_path_addressed(nas, catalog, blobs, facts, tmp_path):
    """Two different filenames with identical bytes share one thumbnail blob."""
    (nas / "Photos" / "dup.jpg").write_bytes((nas / "Photos" / "real.jpg").read_bytes())
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    assert catalog.published_count("nas") == 2
    thumbs = list((blobs.root / "thumbs").iterdir())
    assert len(thumbs) == 1


def test_unindexed_item_without_hash_is_skipped(nas, catalog, blobs, facts):
    """An item with no quick_hash yet (mid-scan) can't be content-addressed."""
    conn = _indexed(nas, catalog)
    catalog.conn.execute(
        "UPDATE items SET quick_hash = NULL WHERE source = 'nas' AND item_id = 'Photos/real.jpg'"
    )
    catalog.conn.commit()

    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    assert result.status == STATUS_NOOP
    assert catalog.published_count("nas") == 0


def test_all_items_failing_surfaces_a_real_reason_not_a_generic_noop(nas, catalog, blobs):
    """Regression: a total failure used to collapse into the same generic
    "no items could be published" message as a legitimate no-op, discarding
    every per-item error — no way to tell "nothing to do" from "everything
    broke" without digging into code neither the CLI nor caller can reach."""
    conn = _indexed(nas, catalog)

    class BrokenFacts:
        name = "broken"
        def put(self, source, item_id, fact):
            raise RuntimeError("Firestore permission denied")

    result = PublishAction("nas", conn, catalog, blobs, BrokenFacts()).run(commit=True)

    assert result.status == STATUS_NOOP
    assert "1 failed" in result.detail
    assert "Firestore permission denied" in result.detail


def test_missing_source_index_fails_validation(tmp_path, catalog, blobs, facts):
    root = tmp_path / "empty_nas"
    root.mkdir()
    conn = NASConnector(str(root))

    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    assert result.status == STATUS_FAILED
    assert "run an index first" in result.error


def test_purge_facts_deletes_only_the_given_source(nas, catalog, blobs, facts, tmp_path):
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    facts.put("drive", "unrelated.jpg", {"source": "drive", "item_id": "unrelated.jpg"})

    deleted = facts.purge("nas")

    assert deleted == 1
    assert not list(facts.root.glob("nas__*.json"))
    assert list(facts.root.glob("drive__*.json"))  # untouched


def test_purge_facts_all_sources(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    facts.put("drive", "unrelated.jpg", {"source": "drive", "item_id": "unrelated.jpg"})

    deleted = facts.purge(None)

    assert deleted == 2
    assert list(facts.root.glob("*.json")) == []


def test_cli_prints_the_actual_reason_for_a_partial_failure(tmp_path, capsys):
    """Regression: a partial failure (some items published, some not) only
    ever printed a bare count — "N item(s) failed — see the journal" — with
    no way to see why short of digging through the action log by hand."""
    from mediavault.cli import main

    nas = tmp_path / "nas"
    (nas / "Photos").mkdir(parents=True)
    (nas / "Photos" / "ok.jpg").write_bytes(_jpeg_bytes())
    (nas / "Photos" / "vanishes.jpg").write_bytes(_jpeg_bytes(color=(1, 2, 3)))
    db = str(tmp_path / "cat.sqlite")

    assert main(["index", "nas", "--root", str(nas), "--db", db, "--quiet"]) == 0
    (nas / "Photos" / "vanishes.jpg").unlink()  # indexed, then removed before publish
    capsys.readouterr()

    exit_code = main([
        "publish", "nas", "--root", str(nas), "--db", db, "--commit",
        "--blob-dir", str(tmp_path / "blobs"), "--facts-dir", str(tmp_path / "facts"),
        "--log-dir", str(tmp_path / "actions"),
    ])

    out = capsys.readouterr().out
    assert exit_code == 0  # a partial failure isn't a command failure
    assert "Published 1 item(s)" in out
    assert "1 item(s) failed:" in out
    assert "Photos/vanishes.jpg" in out
    assert "not found" in out.lower()


# --------------------------------------------------------------------------- #
# Face detection (FACES_LIVE) — gated, best-effort, and must be idempotent
# per item so a --force re-run (e.g. to backfill GPS) doesn't re-detect
# every face and duplicate rows in the local faces table.
# --------------------------------------------------------------------------- #
class _FakeDetectedFace:
    def __init__(self, bbox, embedding, score):
        self.bbox = bbox
        self.embedding = embedding
        # faces.py reads normed_embedding, not embedding (see its own
        # regression test in test_faces.py) — defaulted equal here since
        # these tests care about the detect -> cluster -> publish wiring,
        # not the raw-vs-normalized distinction itself.
        self.normed_embedding = embedding
        self.det_score = score


@pytest.fixture
def fake_insightface(monkeypatch):
    from mediavault import faces as faces_mod

    state = {"faces": [], "calls": 0}

    class FakeFaceAnalysis:
        def __init__(self, name=None, providers=None):
            pass

        def prepare(self, ctx_id, det_size):
            pass

        def get(self, img):
            state["calls"] += 1
            return state["faces"]

    fake_app_module = types.SimpleNamespace(FaceAnalysis=FakeFaceAnalysis)
    monkeypatch.setitem(sys.modules, "insightface", types.SimpleNamespace(app=fake_app_module))
    monkeypatch.setitem(sys.modules, "insightface.app", fake_app_module)
    monkeypatch.setattr(faces_mod, "_app", None)
    return state


def test_faces_live_off_by_default_detects_nothing(nas, catalog, blobs, facts,
                                                    fake_insightface, monkeypatch):
    import numpy as np
    monkeypatch.delenv("FACES_LIVE", raising=False)
    fake_insightface["faces"] = [_FakeDetectedFace(
        (1.0, 2.0, 3.0, 4.0), np.array([0.1, 0.2], dtype="float32"), 0.9)]
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    assert fake_insightface["calls"] == 0
    assert catalog.faces_for_item("nas", "Photos/real.jpg") == []
    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert '"person_ids": []' in fact_file.read_text()


def test_faces_live_detects_and_assigns_a_person(nas, catalog, blobs, facts,
                                                  fake_insightface, monkeypatch):
    import numpy as np
    monkeypatch.setenv("FACES_LIVE", "1")
    fake_insightface["faces"] = [_FakeDetectedFace(
        (1.0, 2.0, 3.0, 4.0), np.array([0.1, 0.2], dtype="float32"), 0.9)]
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    assert fake_insightface["calls"] == 1
    saved_faces = catalog.faces_for_item("nas", "Photos/real.jpg")
    assert len(saved_faces) == 1
    person_id = saved_faces[0]["person_id"]
    assert person_id is not None

    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert f'"{person_id}"' in fact_file.read_text()


def test_faces_live_skips_non_image_items(nas, catalog, blobs, facts,
                                          fake_insightface, monkeypatch):
    import numpy as np
    monkeypatch.setenv("FACES_LIVE", "1")
    fake_insightface["faces"] = [_FakeDetectedFace(
        (1.0, 2.0, 3.0, 4.0), np.array([0.1, 0.2], dtype="float32"), 0.9)]
    (nas / "clip.mov").write_bytes(b"not really a video, just needs a mov extension")
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    assert catalog.faces_for_item("nas", "clip.mov") == []


def test_face_detection_is_idempotent_on_force_republish(nas, catalog, blobs, facts,
                                                          fake_insightface, monkeypatch):
    import numpy as np
    monkeypatch.setenv("FACES_LIVE", "1")
    fake_insightface["faces"] = [_FakeDetectedFace(
        (1.0, 2.0, 3.0, 4.0), np.array([0.1, 0.2], dtype="float32"), 0.9)]
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    assert fake_insightface["calls"] == 1

    # A --force republish (e.g. to backfill an unrelated field like GPS)
    # must not re-run detection or duplicate the face row it already has.
    PublishAction("nas", conn, catalog, blobs, facts, force=True).run(commit=True)

    assert fake_insightface["calls"] == 1  # not called again


# --------------------------------------------------------------------------- #
# Perceptual hash — near-duplicate grouping (web Duplicates tab), always-on
# for images, no live switch needed (cheap, unlike face detection).
# --------------------------------------------------------------------------- #
def test_publish_computes_and_stores_a_phash_for_a_photo(nas, catalog, blobs, facts):
    from mediavault import imaging
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    row = catalog.get("nas", "Photos/real.jpg")
    assert row["phash"] == imaging.phash(_jpeg_bytes())
    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert row["phash"] in fact_file.read_text()


def test_phash_is_reused_not_recomputed_on_force_republish(nas, catalog, blobs, facts, monkeypatch):
    """Unlike EXIF's cheap head-read, phash needs the whole file decoded —
    a --force republish (e.g. to backfill GPS) must reuse the stored value,
    not pay that cost again for an unchanged file."""
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    first = catalog.get("nas", "Photos/real.jpg")["phash"]

    def must_not_be_called(*_a, **_k):
        raise AssertionError("phash() must not run again once already stored")
    monkeypatch.setattr("mediavault.actions.maintenance.imaging.phash", must_not_be_called)

    PublishAction("nas", conn, catalog, blobs, facts, force=True).run(commit=True)

    assert catalog.get("nas", "Photos/real.jpg")["phash"] == first


def test_phash_skips_non_image_items(nas, catalog, blobs, facts):
    (nas / "clip.mov").write_bytes(b"not really a video, just needs a mov extension")
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    assert catalog.get("nas", "clip.mov")["phash"] is None


def test_phash_rides_free_on_the_thumbnails_own_decode(nas, catalog, blobs, facts,
                                                        fake_insightface, monkeypatch):
    """A fresh thumbnail derivation already reads+decodes the whole file —
    phash must reuse that, not pay for a second full read on top of it.
    Face detection (when live) still needs its own separate read, since it
    doesn't share ThumbnailAction's internals — this proves phash isn't
    ALSO adding a third read on top of that."""
    import numpy as np
    monkeypatch.setenv("FACES_LIVE", "1")
    fake_insightface["faces"] = [_FakeDetectedFace(
        (1.0, 2.0, 3.0, 4.0), np.array([0.1, 0.2], dtype="float32"), 0.9)]
    conn = _indexed(nas, catalog)

    full_reads = []
    real_read = conn.read
    def counting_read(item_id, nbytes=0):
        if not nbytes:
            full_reads.append(item_id)
        return real_read(item_id, nbytes)
    monkeypatch.setattr(conn, "read", counting_read)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    # One read for the thumbnail's own decode (phash rides along with it),
    # one for face detection — not three, which is what a phash-specific
    # extra read would cost.
    assert full_reads == ["Photos/real.jpg", "Photos/real.jpg"]
    assert catalog.get("nas", "Photos/real.jpg")["phash"] is not None
    assert fake_insightface["calls"] == 1
    assert len(catalog.faces_for_item("nas", "Photos/real.jpg")) == 1  # not duplicated


def test_phash_backfills_via_its_own_read_when_thumbnail_was_already_stored(nas, catalog, blobs, facts):
    """An item published before phash existed has a thumbnail already (a
    NoOp on republish, so no free decode to ride along with) but no phash —
    a --force republish must still backfill it via a dedicated read."""
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    catalog.conn.execute(
        "UPDATE items SET phash = NULL WHERE source = 'nas' AND item_id = 'Photos/real.jpg'")
    catalog.conn.commit()

    PublishAction("nas", conn, catalog, blobs, facts, force=True).run(commit=True)

    assert catalog.get("nas", "Photos/real.jpg")["phash"] is not None
