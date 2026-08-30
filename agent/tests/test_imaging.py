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
imagehash = pytest.importorskip("imagehash", reason="needs ImageHash (imaging extra)")

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
# phash() — near-duplicate grouping (web Duplicates tab), review-only
# --------------------------------------------------------------------------- #
def _jpeg(im, quality=90) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _hamming(hex_a: str, hex_b: str) -> int:
    return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")


def _gradient_image(size=(200, 200)):
    """A real gradient, not a flat color — dhash compares adjacent-pixel
    brightness, so a single flat color hashes to all-identical bits and
    can't distinguish "similar" from "different" in a meaningful test."""
    im = Image.new("RGB", size)
    for x in range(size[0]):
        shade = int(255 * x / size[0])
        for y in range(size[1]):
            im.putpixel((x, y), (shade, shade, shade))
    return im


def test_phash_returns_a_64_bit_hex_hash():
    out = imaging.phash(_jpeg(_gradient_image()))

    assert len(out) == 16  # 64 bits, 4 bits/hex digit
    int(out, 16)  # doesn't raise — a valid hex string


def test_phash_is_close_for_a_recompressed_near_duplicate():
    """The actual use case: a resize/re-compression of the same photo
    (a messenger-app copy, a re-export) must land close in Hamming
    distance so it groups with the original."""
    im = _gradient_image()
    original = imaging.phash(_jpeg(im, quality=95))
    recompressed = imaging.phash(_jpeg(im, quality=40))

    assert _hamming(original, recompressed) <= 4


def test_phash_is_far_for_visually_different_images():
    a = imaging.phash(_jpeg(_gradient_image()))
    b = imaging.phash(_jpeg(Image.new("RGB", (200, 200), color=(20, 200, 60))))

    assert _hamming(a, b) >= 16


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
