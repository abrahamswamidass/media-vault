"""
imaging.py tests — the one place that decodes a photo.

Added after a real failure: ~40 of 50 real publish attempts failed with
"cannot identify image file" — every one a HEIC, the default iPhone photo
format since iOS 11, which plain Pillow can't open at all without the
pillow-heif plugin registered.
"""
from __future__ import annotations

import io

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
