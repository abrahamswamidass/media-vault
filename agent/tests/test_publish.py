"""
Publish action tests — thumbnails + metadata facts for the (not-yet-built) web
module. Runs with no cloud account: LocalBlobStore/LocalFactsStore stand in for
GCS/Firestore, exactly like LocalBlobStore already stands in for GCS elsewhere.
"""
from __future__ import annotations

import io

import pytest

PIL = pytest.importorskip("PIL", reason="thumbnailing needs Pillow (imaging extra)")
from PIL import Image  # noqa: E402

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


def test_rerun_is_a_noop(nas, catalog, blobs, facts):
    conn = _indexed(nas, catalog)
    PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)

    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    assert result.status == STATUS_NOOP


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


def test_missing_source_index_fails_validation(tmp_path, catalog, blobs, facts):
    root = tmp_path / "empty_nas"
    root.mkdir()
    conn = NASConnector(str(root))

    result = PublishAction("nas", conn, catalog, blobs, facts).run(commit=True)
    assert result.status == STATUS_FAILED
    assert "run an index first" in result.error
