"""
Image derivation — the one place that decodes a photo and makes a smaller one.

Both derived blobs come from here so their sizing and encoding stay consistent:

    thumbnail   400px edge, WebP  ~30 KB   permanent, bulk-pushed
    preview    2048px edge, JPEG ~500 KB   on demand, expires in a day

Pillow is an optional import. The core connectors and CLI are stdlib-only by
design, so anything that needs image decoding says so with a clear error rather
than making Pillow a hard dependency of the whole agent.

Video is handled here too, one step earlier: Pillow can't open a video
container at all ("cannot identify image file", not a clear error — the same
unhelpful failure HEIC gave before pillow-heif was registered). `frame()`
uses ffmpeg (baked into the agent image alongside exiftool — see Dockerfile)
to pull one representative JPEG frame first; from there it's just a photo,
and thumbnail()/preview() don't need to know the source was ever a video.
"""
from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

#: Sizing presets, keyed by the blob "kind" they produce.
THUMB_MAX_EDGE = 400
PREVIEW_MAX_EDGE = 2048


class ImagingUnavailable(RuntimeError):
    """Pillow isn't installed, so this build can't derive images."""


class VideoFrameUnavailable(RuntimeError):
    """ffmpeg isn't installed, or couldn't pull a frame from this file."""


_heif_registered = False


def _register_heif() -> None:
    """Teach Pillow to open HEIC/HEIF — the default photo format on iPhones
    since iOS 11, and otherwise completely opaque to plain Pillow (raises
    "cannot identify image file", not a clear "unsupported format" error).
    Soft-optional like Pillow itself: if pillow-heif isn't installed, HEIC
    files just keep failing with that same unclear error rather than this
    module refusing to load altogether.
    """
    global _heif_registered
    if _heif_registered:
        return
    try:
        import pillow_heif  # noqa: PLC0415 — optional dependency, imported on use

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    _heif_registered = True


def _pillow():
    try:
        from PIL import Image  # noqa: PLC0415 — optional dependency, imported on use
    except ImportError as e:  # pragma: no cover - depends on install profile
        raise ImagingUnavailable(
            "Pillow is required to derive thumbnails/previews. "
            "pip install Pillow (already listed in requirements.txt)."
        ) from e
    _register_heif()
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


def phash(data: bytes) -> str:
    """A 64-bit perceptual hash (difference hash) as a hex string.

    Visually similar images — a resize, a re-compression, a burst-sequence
    shot taken a second apart — land close together in Hamming distance;
    visually different images land far apart. This is the "near" half of
    dedup.py's exact/near distinction: review-only grouping, never an
    automatic action, since picking the wrong one of a near-duplicate pair
    (a full-res original vs. a messenger-app recompression) destroys the
    better file.

    imagehash is a soft-optional import like Pillow itself.
    """
    Image = _pillow()
    try:
        import imagehash  # noqa: PLC0415 — optional dependency, imported on use
    except ImportError as e:  # pragma: no cover - depends on install profile
        raise ImagingUnavailable(
            "ImageHash is required for perceptual hashing. "
            "pip install ImageHash (already listed in requirements.txt)."
        ) from e

    with Image.open(io.BytesIO(data)) as im:
        return str(imagehash.dhash(im))


def frame(data: bytes, suffix: str = "") -> bytes:
    """One representative frame from a video, as JPEG bytes ready for
    downscale()/thumbnail()/preview() to pick up like any photo.

    ffmpeg needs a real file path, not a byte stream, for reliable seeking
    across container formats — same tradeoff metadata.py's exiftool call
    already makes. Seeks 1s in first, since a phone video's very first frame
    is often black or mid-focus-hunt; a clip shorter than that falls back to
    frame zero.
    """
    with tempfile.NamedTemporaryFile(suffix=suffix) as src:
        src.write(data)
        src.flush()
        last_error = "no seek point produced a frame"
        for seek in ("00:00:01", "00:00:00"):
            with tempfile.NamedTemporaryFile(suffix=".jpg") as dst:
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-nostdin", "-loglevel", "error",
                         "-ss", seek, "-i", src.name,
                         "-frames:v", "1", "-q:v", "3", dst.name],
                        check=True, capture_output=True, timeout=30,
                    )
                except FileNotFoundError as e:
                    raise VideoFrameUnavailable(
                        "ffmpeg is not installed — required to thumbnail video files."
                    ) from e
                except subprocess.CalledProcessError as e:
                    last_error = e.stderr.decode(errors="replace").strip()
                    continue
                out = Path(dst.name).read_bytes()
                if out:
                    return out
                last_error = "ffmpeg produced an empty frame"
    raise VideoFrameUnavailable(f"could not extract a frame: {last_error}")
