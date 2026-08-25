"""
EXIF extraction — the one place that reads a photo's embedded metadata.

Mirrors imaging.py's shape: exiftool is an optional extra (the binary is baked
into the Docker image; the PyExifTool wrapper is an optional Python import),
guarded so the core harness stays runnable without it.

PyExifTool's persistent process only reads real file paths, not raw bytes, so
`extract()` writes to a short-lived temp file per call. Callers should pass a
small HEAD read (a NAS file's EXIF block lives near the start — same
"never read a whole file" philosophy as `quick_hash` and `read(nbytes=...)`
elsewhere), not the full file: a 30 MB RAW doesn't need to fully download
just to pull out a date and a camera model.
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from typing import Optional

#: EXIF's own timestamp format: "YYYY:MM:DD HH:MM:SS" (colons, not dashes, in
#: the date part — a long-standing EXIF spec quirk).
_EXIF_DATE_FMT = "%Y:%m:%d %H:%M:%S"

_TAGS = ["File:ImageWidth", "File:ImageHeight",
         "EXIF:DateTimeOriginal", "EXIF:Make", "EXIF:Model"]


class MetadataUnavailable(RuntimeError):
    """PyExifTool isn't installed, so this build can't extract EXIF."""


_helper = None


def _exiftool_helper():
    global _helper
    try:
        import exiftool  # noqa: PLC0415 — optional dependency, imported on use
    except ImportError as e:  # pragma: no cover - depends on install profile
        raise MetadataUnavailable(
            "PyExifTool is required to extract EXIF metadata. "
            "pip install PyExifTool (already listed in requirements.txt)."
        ) from e
    if _helper is None:
        _helper = exiftool.ExifToolHelper()
    return _helper


def _parse_date(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.strptime(value, _EXIF_DATE_FMT).timestamp()
    except ValueError:
        return None


def extract(data: bytes, suffix: str = "") -> dict:
    """Pull a few key fields out of a (partial) file's bytes.

    Returns {} on any failure or absence rather than raising — most photos
    have partial or no EXIF (screenshots, edited exports, a truncated read
    that cut through the metadata block), and that's an expected outcome,
    not an error worth surfacing to the caller.
    """
    helper = _exiftool_helper()
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            results = helper.get_tags([tmp.name], tags=_TAGS)
        except Exception:
            return {}

    if not results:
        return {}
    raw = results[0]
    return {
        "width": raw.get("File:ImageWidth"),
        "height": raw.get("File:ImageHeight"),
        "date_taken": _parse_date(raw.get("EXIF:DateTimeOriginal")),
        "camera_make": raw.get("EXIF:Make"),
        "camera_model": raw.get("EXIF:Model"),
    }
