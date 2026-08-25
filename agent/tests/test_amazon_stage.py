"""
StageForAmazonAction tests — closes issue #8: staging a NAS item for Amazon
without needing it on the container's own filesystem first.
"""
from __future__ import annotations

import pytest

from mediavault.actions import STATUS_FAILED, STATUS_OK
from mediavault.actions.amazon import StageForAmazonAction
from mediavault.connectors.amazon import AmazonConnector
from mediavault.connectors.nas import NASConnector


@pytest.fixture
def nas(tmp_path):
    root = tmp_path / "nas"
    (root / "Photos").mkdir(parents=True)
    (root / "Photos" / "vacation.jpg").write_bytes(b"fake-photo-bytes")
    return NASConnector(str(root))


@pytest.fixture
def amazon(tmp_path):
    root = tmp_path / "amazon_staging"
    root.mkdir()
    return AmazonConnector(str(root))


def test_dry_run_stages_nothing(nas, amazon):
    result = StageForAmazonAction("Photos/vacation.jpg", nas, amazon).run(commit=False)

    assert result.status == STATUS_OK
    assert not result.committed
    assert list(amazon._fs.root.iterdir()) == []


def test_commit_preserves_original_filename(nas, amazon):
    """Regression: the temp file used to carry a random name (tmp55g30dp3.jpg)
    straight into the dated album folder instead of the real filename, because
    AmazonConnector.upload()'s dated-album default derives the name from
    local_path's own basename."""
    result = StageForAmazonAction("Photos/vacation.jpg", nas, amazon).run(commit=True)

    assert result.status == STATUS_OK
    staged = list(amazon._fs.root.glob("*/vacation.jpg"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"fake-photo-bytes"


def test_original_is_left_on_the_nas(nas, amazon):
    StageForAmazonAction("Photos/vacation.jpg", nas, amazon).run(commit=True)

    assert (nas.root / "Photos" / "vacation.jpg").exists()


def test_missing_source_item_fails_validation(nas, amazon):
    result = StageForAmazonAction("Photos/does-not-exist.jpg", nas, amazon).run(commit=True)

    assert result.status == STATUS_FAILED
    assert "not found" in result.error


def test_directory_is_refused(nas, amazon):
    result = StageForAmazonAction("Photos", nas, amazon).run(commit=True)

    assert result.status == STATUS_FAILED
    assert "directory" in result.error
