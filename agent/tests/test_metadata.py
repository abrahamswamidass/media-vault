"""
EXIF extraction tests. The real `exiftool` binary isn't available in CI (or
this dev sandbox), so these inject a fake `exiftool` Python module — only the
field-mapping and date-parsing logic in metadata.py is under test here, not
the real PyExifTool/exiftool integration (that's exercised manually against
the Docker image, which does have both).
"""
from __future__ import annotations

import sys
import types
from datetime import datetime

import pytest

from mediavault import metadata


@pytest.fixture
def fake_exiftool(monkeypatch):
    """Injects a fake `exiftool` module and resets metadata's cached helper
    singleton, so each test starts clean regardless of import order."""
    state = {"result": []}

    class FakeExifToolHelper:
        def get_tags(self, files, tags):
            state["files"] = files
            state["tags"] = tags
            return state["result"]

    fake_module = types.SimpleNamespace(ExifToolHelper=FakeExifToolHelper)
    monkeypatch.setitem(sys.modules, "exiftool", fake_module)
    monkeypatch.setattr(metadata, "_helper", None)
    return state


def test_extract_maps_known_fields(fake_exiftool):
    fake_exiftool["result"] = [{
        "File:ImageWidth": 6000, "File:ImageHeight": 4000,
        "EXIF:DateTimeOriginal": "2026:01:15 10:30:00",
        "EXIF:Make": "Canon", "EXIF:Model": "EOS R5",
    }]

    result = metadata.extract(b"fake bytes", suffix=".CR2")

    assert result["width"] == 6000
    assert result["height"] == 4000
    assert result["camera_make"] == "Canon"
    assert result["camera_model"] == "EOS R5"
    expected = datetime.strptime("2026:01:15 10:30:00", "%Y:%m:%d %H:%M:%S").timestamp()
    assert result["date_taken"] == expected


def test_extract_handles_missing_fields(fake_exiftool):
    """Most photos have partial or no EXIF — absent fields are None, not an error."""
    fake_exiftool["result"] = [{}]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result == {
        "width": None, "height": None, "date_taken": None,
        "camera_make": None, "camera_model": None,
    }


def test_extract_handles_no_results(fake_exiftool):
    fake_exiftool["result"] = []

    assert metadata.extract(b"fake", suffix=".jpg") == {}


def test_extract_handles_unparsable_date(fake_exiftool):
    fake_exiftool["result"] = [{"EXIF:DateTimeOriginal": "not-a-date"}]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result["date_taken"] is None


def test_missing_pyexiftool_raises_metadata_unavailable(monkeypatch):
    # A None entry in sys.modules is the standard way to force `import x` to
    # raise ImportError without needing the real package absent from the env.
    monkeypatch.setitem(sys.modules, "exiftool", None)
    monkeypatch.setattr(metadata, "_helper", None)

    with pytest.raises(metadata.MetadataUnavailable):
        metadata.extract(b"fake")
