"""
Action layer tests — the dry-run gate, the audit journal, and idempotency.

These run with no cloud account and no Docker: LocalBlobStore stands in for GCS
and a temp folder stands in for the NAS.
"""
from __future__ import annotations

import pytest

from mediavault.actions import (
    ActionLog, CopyAction, DeleteAction, MoveAction,
    STATUS_FAILED, STATUS_NOOP, STATUS_OK,
)
from mediavault.actions.derive import FetchFullResAction
from mediavault.connectors.nas import NASConnector
from mediavault.blobstore import LocalBlobStore, blob_key


@pytest.fixture
def nas(tmp_path):
    root = tmp_path / "nas"
    (root / "Photos").mkdir(parents=True)
    (root / "Photos" / "img_001.jpg").write_bytes(b"pretend-jpeg-bytes")
    (root / "Photos" / "junk.jpg").write_bytes(b"delete-me")
    return NASConnector(str(root))


@pytest.fixture
def staging(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    return NASConnector(str(root))


@pytest.fixture
def blobs(tmp_path):
    return LocalBlobStore(str(tmp_path / "blobs"))


# --------------------------------------------------------------------------- #
# The dry-run gate
# --------------------------------------------------------------------------- #
def test_dry_run_is_the_default(nas):
    """Forgetting the flag must preview, never mutate."""
    target = nas.root / "Photos" / "junk.jpg"

    result = DeleteAction("Photos/junk.jpg", nas).run()   # no commit=

    assert result.status == STATUS_OK
    assert result.committed is False
    assert "DRY-RUN" in result.detail
    assert target.exists(), "dry-run must not touch the file"


def test_commit_soft_deletes_to_trash(nas):
    result = DeleteAction("Photos/junk.jpg", nas).run(commit=True)

    assert result.status == STATUS_OK
    assert result.committed is True
    assert not (nas.root / "Photos" / "junk.jpg").exists()
    assert (nas.trash / "Photos" / "junk.jpg").exists(), "delete must be reversible"


def test_validation_failure_never_reaches_execute(nas):
    result = DeleteAction("Photos/does_not_exist.jpg", nas).run(commit=True)

    assert result.status == STATUS_FAILED
    assert result.committed is False
    assert "not found" in result.error


def test_copy_leaves_the_source_alone(nas, staging):
    result = CopyAction("Photos/img_001.jpg", nas, staging,
                        dest_path="2026-01/img_001.jpg").run(commit=True)

    assert result.status == STATUS_OK
    assert (nas.root / "Photos" / "img_001.jpg").exists()
    assert (staging.root / "2026-01" / "img_001.jpg").read_bytes() == b"pretend-jpeg-bytes"


def test_move_copies_before_deleting(nas, staging):
    """A move that half-fails must leave the file somewhere, never nowhere."""
    result = MoveAction("Photos/img_001.jpg", nas, staging).run(commit=True)

    assert result.status == STATUS_OK
    assert (staging.root / "img_001.jpg").exists()
    assert (nas.trash / "Photos" / "img_001.jpg").exists()
    assert not (nas.root / "Photos" / "img_001.jpg").exists()


# --------------------------------------------------------------------------- #
# Full-res fetch — the on-demand cloud path
# --------------------------------------------------------------------------- #
def test_fetch_original_is_content_addressed(nas, blobs):
    action = FetchFullResAction("Photos/img_001.jpg", nas, blobs, variant="original")
    result = action.run(commit=True)

    assert result.status == STATUS_OK
    expected = blob_key(nas.stat("Photos/img_001.jpg").quick_hash,
                        "previews/original", "jpg")
    assert result.outputs["key"] == expected
    assert blobs.exists(expected)


def test_replayed_fetch_is_a_noop(nas, blobs):
    """At-least-once intent delivery means this action WILL run twice."""
    first = FetchFullResAction("Photos/img_001.jpg", nas, blobs,
                               variant="original").run(commit=True)
    second = FetchFullResAction("Photos/img_001.jpg", nas, blobs,
                                variant="original").run(commit=True)

    assert first.status == STATUS_OK and first.committed is True
    assert second.status == STATUS_NOOP, "a replay must not re-read the NAS"
    assert second.committed is False


def test_unknown_variant_is_refused(nas, blobs):
    result = FetchFullResAction("Photos/img_001.jpg", nas, blobs,
                                variant="raw-4k").run(commit=True)

    assert result.status == STATUS_FAILED
    assert "unknown variant" in result.error


# --------------------------------------------------------------------------- #
# The journal
# --------------------------------------------------------------------------- #
def test_log_records_enough_to_replay(nas, tmp_path):
    log = ActionLog(str(tmp_path / "journal"))
    log.record(DeleteAction("Photos/junk.jpg", nas).run(commit=True))

    entry = log.last_for("Photos/junk.jpg")
    assert entry.action_type == "delete"
    assert entry.inputs == {"connector": "nas", "item_id": "Photos/junk.jpg"}
    assert entry.outputs["dest"].endswith("_trash/Photos/junk.jpg")


def test_log_survives_a_torn_final_line(nas, tmp_path):
    """A crash mid-write should cost the last line, not the whole journal."""
    log = ActionLog(str(tmp_path / "journal"))
    log.record(DeleteAction("Photos/junk.jpg", nas).run(commit=True))
    with log.path.open("a") as f:
        f.write('{"action_type": "delete", "stat')      # truncated

    assert len(list(log)) == 1


def test_summary_counts_by_type_and_status(nas, staging, tmp_path):
    log = ActionLog(str(tmp_path / "journal"))
    log.record(DeleteAction("Photos/junk.jpg", nas).run(commit=True))
    log.record(CopyAction("Photos/img_001.jpg", nas, staging).run(commit=True))
    log.record(DeleteAction("Photos/gone.jpg", nas).run(commit=True))   # fails

    summary = log.summary()
    assert summary["total"] == 3
    assert summary["by_type"] == {"delete": 2, "copy": 1}
    assert summary["by_status"] == {"ok": 2, "failed": 1}
    assert len(log.failures()) == 1
