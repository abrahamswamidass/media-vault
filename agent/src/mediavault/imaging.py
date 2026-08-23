"""
Image derivation — the one place that decodes a photo and makes a smaller one.

Both derived blobs come from here so their sizing and encoding stay consistent:

    thumbnail   400px edge, WebP  ~30 KB   permanent, bulk-pushed
    preview    2048px edge, JPEG ~500 KB   on demand, expires in a day

Pillow is an optional import. The core connectors and CLI are stdlib-only by
design, so anything that needs image decoding says so with a clear error rather
than making Pillow a hard dependency of the whole agent.
"""
from __future__ import annotations

import io

#: Sizing presets, keyed by the blob "kind" they produce.
THUMB_MAX_EDGE = 400
PREVIEW_MAX_EDGE = 2048


class ImagingUnavailable(RuntimeError):
    """Pillow isn't installed, so this build can't derive images."""


def _pillow():
    try:
        from PIL import Image  # noqa: PLC0415 — optional dependency, imported on use
    except ImportError as e:  # pragma: no cover - depends on install profile
        raise ImagingUnavailable(
            "Pillow is required to derive thumbnails/previews. "
            "pip install Pillow (already listed in requirements.txt)."
        ) from e
    return Image


def downscale(data: bytes, max_edge: int, fmt: str = "WEBP", quality: int = 82) -> bytes:
    """Shrink an image so its longest edge is `max_edge`, and re-encode it.

    Never upscales: an image already smaller than `max_edge` is re-encoded at its
    original size. EXIF orientation is applied and then dropped, so the derived
    image is upright and carries no location metadata into the cloud.
    """
    Image = _pillow()
    from PIL import ImageOps

    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)          # bake in rotation, then forget it
        im.thumbnail((max_edge, max_edge), Image.LANCZOS)   # no-ops if already smaller
        if fmt.upper() in {"JPEG", "WEBP"} and im.mode not in {"RGB", "L"}:
            im = im.convert("RGB")                # drop alpha; JPEG can't carry it
        out = io.BytesIO()
        im.save(out, format=fmt.upper(), quality=quality)
        return out.getvalue()


def thumbnail(data: bytes) -> bytes:
    """400px WebP — what the web grid renders."""
    return downscale(data, THUMB_MAX_EDGE, fmt="WEBP", quality=80)


def preview(data: bytes) -> bytes:
    """2048px JPEG — enough to actually look at a photo and judge it."""
    return downscale(data, PREVIEW_MAX_EDGE, fmt="JPEG", quality=85)
