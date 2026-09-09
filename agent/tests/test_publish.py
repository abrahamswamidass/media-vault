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
    # NASConnector (the mount-based connector) stringifies item_id via
    # Path.relative_to(), which uses the OS-native separator -- "/" on
    # Linux (always true in the real container, and for the SMB connector
    # production actually uses), backslashes if this suite runs directly
    # on a Windows host. Normalizing here matches what every real
    # deployment actually sees -- same fix as test_dedup.py's
    # _normalize_item_ids, this file just never exercised it before now
    # (it's Pillow-gated, and Pillow wasn't installed locally until this
    # session's face-crop work needed to actually verify against it).
    catalog.conn.execute(
        "UPDATE items SET item_id = REPLACE(item_id, '\\', '/') WHERE source = ?",
        (source,))
    catalog.conn.commit()
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


def test_video_exif_reuses_the_original_container_not_the_extracted_frame(
        nas, catalog, blobs, facts, fake_exiftool, monkeypatch):
    """A video's `full` (see ThumbnailAction.raw) is the untouched container
    bytes, never the one JPEG frame extracted for thumbnailing -- exiftool
    needs the real container to read Duration/QuickTime tags at all, so EXIF
    must reuse THAT, not the frame, and must not pay for a second read to
    get it."""
    (nas / "clip.mov").write_bytes(b"pretend-video-bytes")
    monkeypatch.setattr("mediavault.actions.derive.imaging.frame",
                        lambda data, suffix="": b"fake-frame-jpeg")
    monkeypatch.setattr("mediavault.actions.derive.imaging.thumbnail",
                        lambda data: b"fake-webp")
    fake_exiftool["result"] = [{"Composite:Duration": 12.34}]
    conn = _indexed(nas, catalog)

    full_reads, head_reads = [], []
    real_read = conn.read
    def counting_read(item_id, nbytes=0):
        (full_reads if not nbytes else head_reads).append(item_id)
        return real_read(item_id, nbytes)
    monkeypatch.setattr(conn, "read", counting_read)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    row = catalog.get("nas", "clip.mov")
    assert row["duration_seconds"] == 12.34
    # nas/ also has the fixture's own Photos/real.jpg pending -- only care
    # about how many times THIS item's bytes were fetched: once, reused for
    # EXIF, not a second dedicated head-read on top of it.
    assert full_reads.count("clip.mov") == 1
    assert "clip.mov" not in head_reads


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


def test_facts_delete_removes_one_items_document(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    fact_file = facts.root / "nas__Photos_real.jpg.json"
    assert fact_file.exists()

    facts.delete("nas", "Photos/real.jpg")

    assert not fact_file.exists()


def test_facts_delete_of_an_already_missing_document_is_not_an_error(facts):
    """Safe to retry: a replayed "delete" intent (see ArchiveItemAction)
    must not fail just because a previous attempt already removed it."""
    facts.delete("nas", "never/existed.jpg")  # must not raise


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
    # NASConnector's error message embeds the item's own path, which is
    # legitimately OS-native (a real filesystem-facing string, unlike
    # item_id elsewhere) -- backslashes on Windows. Normalize before
    # comparing, same reasoning as test_actions.py's dest-path assertion.
    assert "Photos/vanishes.jpg" in out.replace("\\", "/")
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
# `faces`: person_id + normalized bbox, the one deliberate exception to
# "no biometric data leaves the agent" -- see CLAUDE.md's design note.
# Only the box crosses to Firestore, never the embedding.
#
# Exercised via a pre-seeded catalog.add_face() row rather than the
# fake_insightface detect/assign pipeline above: this is exactly the
# "already detected" path (need_faces=False, since `existing` is non-empty)
# every re-publish of a previously-detected item actually takes, and it
# isolates the normalization logic itself from the detect->assign->store
# pipeline those other tests already cover.
# --------------------------------------------------------------------------- #
def test_faces_field_carries_a_bbox_normalized_to_image_dimensions(
        nas, catalog, blobs, facts, fake_exiftool):
    import json
    fake_exiftool["result"] = [{"File:ImageWidth": 800, "File:ImageHeight": 600}]
    conn = _indexed(nas, catalog)
    # A bbox of (80,60,240,180) on an 800x600 image should normalize to
    # (0.1, 0.1, 0.3, 0.3).
    catalog.add_face("nas", "Photos/real.jpg", (80.0, 60.0, 240.0, 180.0), 0.9, b"\x00" * 4, 1)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    fact = json.loads((facts.root / "nas__Photos_real.jpg.json").read_text())
    assert len(fact["faces"]) == 1
    entry = fact["faces"][0]
    assert entry["person_id"] == "1"
    assert entry["person_id"] == fact["person_ids"][0]
    assert entry["bbox"] == pytest.approx([0.1, 0.1, 0.3, 0.3])
    assert entry["score"] == pytest.approx(0.9)


def test_faces_field_carries_the_detector_score_from_a_fresh_detection(
        nas, catalog, blobs, facts, fake_insightface, monkeypatch):
    """The web uses this to distinguish a real face from a likely false
    positive (a pattern/texture the model mistook for one) -- must be
    published from a live detection, not just the already-detected path
    the test above covers."""
    import json
    import numpy as np
    monkeypatch.setenv("FACES_LIVE", "1")
    fake_insightface["faces"] = [_FakeDetectedFace(
        (1.0, 2.0, 3.0, 4.0), np.array([0.1, 0.2], dtype="float32"), 0.42)]
    conn = _indexed(nas, catalog)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    fact = json.loads((facts.root / "nas__Photos_real.jpg.json").read_text())
    assert fact["faces"][0]["score"] == pytest.approx(0.42)


def test_faces_field_bbox_is_null_without_known_image_dimensions(
        nas, catalog, blobs, facts):
    """No EXIF (no fake_exiftool fixture here, so extraction yields {}) ->
    no width/height to normalize against -> bbox stays null rather than a
    meaningless or wrong fraction. Still publishes the person_id either way
    -- a missing crop hint should never block the rest of publishing."""
    import json
    conn = _indexed(nas, catalog)
    catalog.add_face("nas", "Photos/real.jpg", (80.0, 60.0, 240.0, 180.0), 0.9, b"\x00" * 4, 1)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    fact = json.loads((facts.root / "nas__Photos_real.jpg.json").read_text())
    assert fact["faces"] == [{"person_id": "1", "bbox": None, "score": pytest.approx(0.9)}]


def test_faces_field_is_never_missing_the_embedding_only_the_bbox(
        nas, catalog, blobs, facts, fake_exiftool):
    """The whole point: person_id + location crosses to Firestore, the
    embedding itself never does, under any key."""
    fake_exiftool["result"] = [{"File:ImageWidth": 800, "File:ImageHeight": 600}]
    conn = _indexed(nas, catalog)
    real_embedding = b"\x01\x02\x03\x04" * 32
    catalog.add_face("nas", "Photos/real.jpg", (80.0, 60.0, 240.0, 180.0), 0.9, real_embedding, 1)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    raw = (facts.root / "nas__Photos_real.jpg.json").read_text()
    assert "embedding" not in raw
    assert "\\u0001\\u0002\\u0003\\u0004" not in raw


def test_faces_field_still_populates_on_a_force_republish(
        nas, catalog, blobs, facts, fake_exiftool):
    """A --force republish of an item whose faces were already detected in
    an earlier run (need_faces=False, so detection itself doesn't re-run)
    must still emit `faces` from the existing rows, not leave it empty."""
    import json
    fake_exiftool["result"] = [{"File:ImageWidth": 800, "File:ImageHeight": 600}]
    conn = _indexed(nas, catalog)
    catalog.add_face("nas", "Photos/real.jpg", (80.0, 60.0, 240.0, 180.0), 0.9, b"\x00" * 4, 1)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    PublishAction("nas", conn, catalog, blobs, facts, force=True).run(commit=True)

    fact = json.loads((facts.root / "nas__Photos_real.jpg.json").read_text())
    assert len(fact["faces"]) == 1
    assert fact["faces"][0]["bbox"] == pytest.approx([0.1, 0.1, 0.3, 0.3])


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
    """A fresh thumbnail derivation already reads the whole file (see
    ThumbnailAction.raw) — phash and face detection must both reuse those
    same bytes, not each pay for their own separate read on top of it (nor
    a dedicated EXIF read either — see the head-slice in maintenance.py)."""
    import numpy as np
    monkeypatch.setenv("FACES_LIVE", "1")
    fake_insightface["faces"] = [_FakeDetectedFace(
        (1.0, 2.0, 3.0, 4.0), np.array([0.1, 0.2], dtype="float32"), 0.9)]
    conn = _indexed(nas, catalog)

    full_reads, head_reads = [], []
    real_read = conn.read
    def counting_read(item_id, nbytes=0):
        (full_reads if not nbytes else head_reads).append(item_id)
        return real_read(item_id, nbytes)
    monkeypatch.setattr(conn, "read", counting_read)

    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    # Exactly one read, full stop — thumbnailing, EXIF, phash, and face
    # detection all share it, instead of each fetching the same file again
    # (up to 3 full reads plus a dedicated EXIF head-read, before this).
    assert full_reads == ["Photos/real.jpg"]
    assert head_reads == []
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
