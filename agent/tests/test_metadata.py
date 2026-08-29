"""
EXIF extraction tests. The real `exiftool` binary isn't available in CI (or
this dev sandbox), so these inject a fake `exiftool` Python module — only the
field-mapping, date-fallback, and duration-parsing logic in metadata.py is
under test here, not the real PyExifTool/exiftool integration (that's
exercised manually against the Docker image, which does have both).
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
        def get_tags(self, files, tags, params=None):
            state["files"] = files
            state["tags"] = tags
            state["params"] = params
            return state["result"]

    fake_module = types.SimpleNamespace(ExifToolHelper=FakeExifToolHelper)
    monkeypatch.setitem(sys.modules, "exiftool", fake_module)
    monkeypatch.setattr(metadata, "_helper", None)
    return state


def _epoch(s: str) -> float:
    return datetime.strptime(s, "%Y:%m:%d %H:%M:%S").timestamp()


def test_extract_maps_known_fields(fake_exiftool):
    fake_exiftool["result"] = [{
        "File:ImageWidth": 6000, "File:ImageHeight": 4000,
        "EXIF:DateTimeOriginal": "2026:01:15 10:30:00",
        "EXIF:Make": "Canon", "EXIF:Model": "EOS R5",
        "Composite:GPSLatitude": 37.7749, "Composite:GPSLongitude": -122.4194,
    }]

    result = metadata.extract(b"fake bytes", suffix=".CR2")

    assert result["width"] == 6000
    assert result["height"] == 4000
    assert result["camera_make"] == "Canon"
    assert result["camera_model"] == "EOS R5"
    assert result["latitude"] == 37.7749
    assert result["longitude"] == -122.4194
    assert result["date_taken"] == _epoch("2026:01:15 10:30:00")
    assert result["duration_seconds"] is None


def test_extract_handles_missing_fields(fake_exiftool):
    """Most photos have partial or no EXIF — absent fields are None, not an error."""
    fake_exiftool["result"] = [{}]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result == {
        "width": None, "height": None, "date_taken": None,
        "camera_make": None, "camera_model": None,
        "latitude": None, "longitude": None, "duration_seconds": None,
    }


def test_extract_handles_no_results(fake_exiftool):
    fake_exiftool["result"] = []

    assert metadata.extract(b"fake", suffix=".jpg") == {}


def test_extract_requests_numeric_mode(fake_exiftool):
    """-n is what makes Duration come back as a plain number instead of a
    print-converted string like "0:00:12" — GPS/date tags are unaffected
    either way, but this is the flag that makes the duration test below
    meaningful against the real exiftool binary, not just this fake."""
    fake_exiftool["result"] = [{}]

    metadata.extract(b"fake", suffix=".jpg")

    assert fake_exiftool["params"] == ["-n"]


def test_missing_pyexiftool_raises_metadata_unavailable(monkeypatch):
    # A None entry in sys.modules is the standard way to force `import x` to
    # raise ImportError without needing the real package absent from the env.
    monkeypatch.setitem(sys.modules, "exiftool", None)
    monkeypatch.setattr(metadata, "_helper", None)

    with pytest.raises(metadata.MetadataUnavailable):
        metadata.extract(b"fake")


# --------------------------------------------------------------------------- #
# date_taken fallback chain: DateTimeOriginal -> CreateDate -> QuickTime:CreateDate,
# each only used if it passes the implausible-date guard (a camera with a dead
# clock battery is a real failure mode, not a hypothetical one).
# --------------------------------------------------------------------------- #
def test_date_prefers_datetimeoriginal_over_the_others(fake_exiftool):
    fake_exiftool["result"] = [{
        "EXIF:DateTimeOriginal": "2026:01:15 10:30:00",
        "EXIF:CreateDate": "2026:01:16 00:00:00",
        "QuickTime:CreateDate": "2026:01:17 00:00:00",
    }]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result["date_taken"] == _epoch("2026:01:15 10:30:00")


def test_date_falls_back_to_createdate_when_original_is_absent(fake_exiftool):
    fake_exiftool["result"] = [{"EXIF:CreateDate": "2026:01:16 00:00:00"}]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result["date_taken"] == _epoch("2026:01:16 00:00:00")


def test_date_falls_back_to_quicktime_createdate_for_video(fake_exiftool):
    fake_exiftool["result"] = [{"QuickTime:CreateDate": "2026:01:17 00:00:00"}]

    result = metadata.extract(b"fake", suffix=".mov")

    assert result["date_taken"] == _epoch("2026:01:17 00:00:00")


def test_date_skips_an_implausible_datetimeoriginal(fake_exiftool):
    """A dead clock battery is a real, common failure mode — the camera
    resets to a manufacture-era or epoch default date. That shouldn't win
    over a plausible date from a different tag."""
    fake_exiftool["result"] = [{
        "EXIF:DateTimeOriginal": "1970:01:01 00:00:00",
        "EXIF:CreateDate": "2026:01:16 00:00:00",
    }]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result["date_taken"] == _epoch("2026:01:16 00:00:00")


def test_date_is_none_when_every_source_is_implausible_or_absent(fake_exiftool):
    fake_exiftool["result"] = [{"EXIF:DateTimeOriginal": "1970:01:01 00:00:00"}]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result["date_taken"] is None


def test_extract_handles_unparsable_date(fake_exiftool):
    fake_exiftool["result"] = [{"EXIF:DateTimeOriginal": "not-a-date"}]

    result = metadata.extract(b"fake", suffix=".jpg")

    assert result["date_taken"] is None


# --------------------------------------------------------------------------- #
# duration_seconds — video only, best-effort within the same head-read
# budget as everything else (see the file header on why a video's duration
# atom can legitimately be outside that window).
# --------------------------------------------------------------------------- #
def test_duration_parses_a_plain_number(fake_exiftool):
    fake_exiftool["result"] = [{"Composite:Duration": 12.34}]

    result = metadata.extract(b"fake", suffix=".mov")

    assert result["duration_seconds"] == 12.34


def test_duration_parses_a_formatted_seconds_string(fake_exiftool):
    """Some exiftool versions/containers print-convert Duration even with -n
    for certain composite forms — best-effort parse either way."""
    fake_exiftool["result"] = [{"Composite:Duration": "12.3 s"}]

    result = metadata.extract(b"fake", suffix=".mov")

    assert result["duration_seconds"] == pytest.approx(12.3)


def test_duration_parses_a_hh_mm_ss_string(fake_exiftool):
    fake_exiftool["result"] = [{"Composite:Duration": "0:01:05"}]

    result = metadata.extract(b"fake", suffix=".mov")

    assert result["duration_seconds"] == pytest.approx(65.0)


def test_duration_is_none_when_the_moov_atom_missed_the_head_read(fake_exiftool):
    """The common case for a large, non-"fast start" video: no Duration tag
    at all in what exiftool could see. Absence, not a parse failure."""
    fake_exiftool["result"] = [{}]

    result = metadata.extract(b"fake", suffix=".mov")

    assert result["duration_seconds"] is None
