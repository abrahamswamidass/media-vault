"""
imaging.py tests — the one place that decodes a photo.

Added after a real failure: ~40 of 50 real publish attempts failed with
"cannot identify image file" — every one a HEIC, the default iPhone photo
format since iOS 11, which plain Pillow can't open at all without the
pillow-heif plugin registered.

frame()'s own tests below were added after the same failure mode hit video:
8 of 50 publish attempts failed the same "cannot identify image file" way,
every one a .MOV/.mp4 that was never routed through anything but Pillow.
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL", reason="needs Pillow (imaging extra)")
from PIL import Image  # noqa: E402

pillow_heif = pytest.importorskip("pillow_heif", reason="needs pillow-heif (imaging extra)")

from mediavault import imaging  # noqa: E402


def _heic_bytes(color=(120, 180, 90), size=(800, 600)) -> bytes:
    im = Image.new("RGB", size, color=color)
    heif_file = pillow_heif.from_pillow(im)
    buf = io.BytesIO()
    heif_file.save(buf, quality=80)
    return buf.getvalue()


def test_thumbnail_decodes_a_real_heic_file():
    data = _heic_bytes()

    out = imaging.thumbnail(data)

    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "WEBP"
        assert max(im.size) == imaging.THUMB_MAX_EDGE


def test_preview_decodes_a_real_heic_file():
    data = _heic_bytes()

    out = imaging.preview(data)

    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "JPEG"


def test_still_decodes_plain_jpeg_after_heif_registration():
    """Registering the HEIF opener must not interfere with formats Pillow
    already handled natively."""
    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color=(10, 20, 30)).save(buf, format="JPEG")

    out = imaging.thumbnail(buf.getvalue())

    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "WEBP"


# --------------------------------------------------------------------------- #
# frame() — video handled one step before Pillow ever sees it. ffmpeg itself
# is mocked out via subprocess.run rather than required in the test
# environment, same seam metadata.py's tests use for exiftool.
# --------------------------------------------------------------------------- #
def _fake_ffmpeg(write_at_seek: dict[str, bytes | None]):
    """Return a subprocess.run replacement that, per seek point, either
    writes given bytes to the destination path or raises CalledProcessError
    (when the mapped value is None)."""
    def fake_run(cmd, check, capture_output, timeout):
        seek = cmd[cmd.index("-ss") + 1]
        payload = write_at_seek.get(seek)
        if payload is None:
            raise subprocess.CalledProcessError(1, cmd, stderr=b"invalid data found")
        Path(cmd[-1]).write_bytes(payload)
        return subprocess.CompletedProcess(cmd, 0)
    return fake_run


def test_frame_extracts_at_one_second_by_default(monkeypatch):
    monkeypatch.setattr(imaging.subprocess, "run",
                        _fake_ffmpeg({"00:00:01": b"frame-at-1s"}))

    out = imaging.frame(b"pretend-video-bytes", suffix=".mp4")

    assert out == b"frame-at-1s"


def test_frame_falls_back_to_zero_when_one_second_is_past_the_clip(monkeypatch):
    """A clip shorter than 1s must still produce a thumbnail, not fail
    outright just because the first seek point doesn't exist."""
    monkeypatch.setattr(imaging.subprocess, "run",
                        _fake_ffmpeg({"00:00:01": None, "00:00:00": b"frame-at-0s"}))

    out = imaging.frame(b"short-clip-bytes", suffix=".mov")

    assert out == b"frame-at-0s"


def test_frame_raises_a_clear_error_when_ffmpeg_is_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("no such file: 'ffmpeg'")
    monkeypatch.setattr(imaging.subprocess, "run", fake_run)

    with pytest.raises(imaging.VideoFrameUnavailable, match="ffmpeg is not installed"):
        imaging.frame(b"pretend-video-bytes", suffix=".mp4")


def test_frame_raises_with_ffmpegs_own_error_when_every_seek_fails(monkeypatch):
    monkeypatch.setattr(imaging.subprocess, "run",
                        _fake_ffmpeg({"00:00:01": None, "00:00:00": None}))

    with pytest.raises(imaging.VideoFrameUnavailable, match="invalid data found"):
        imaging.frame(b"corrupt-bytes", suffix=".mp4")
