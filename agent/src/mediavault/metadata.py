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

That same HEAD-read budget is why `duration_seconds` (video only) is
best-effort in a different way than everything else here: a video's duration
lives in its container's moov atom, which some recording tools write at the
very END of the file (only "fast start" / streaming-optimized files put it up
front). A video whose moov atom didn't make it into the head read just comes
back with no duration — expected, not a bug — reading further to chase it
would mean downloading arbitrarily large video files just for a number, which
is exactly the memory/bandwidth cost this project avoids everywhere else.
"""
from __future__ import annotations

import tempfile
import time
from datetime import datetime
from typing import Optional

#: EXIF's own timestamp format: "YYYY:MM:DD HH:MM:SS" (colons, not dashes, in
#: the date part — a long-standing EXIF spec quirk). exiftool normalizes
#: QuickTime's own date tags to this same format, so one parser covers both.
_EXIF_DATE_FMT = "%Y:%m:%d %H:%M:%S"

#: Digital cameras (let alone phones) predating this are vanishingly rare in
#: a personal library — a date before it almost always means a camera whose
#: clock reset to its manufacture/epoch default (a dead clock battery is a
#: real, common failure mode), not a genuine capture date. The plausibility
#: guard below only exists to catch that, not to be a real cutoff.
_MIN_PLAUSIBLE_YEAR = 1995

#: Composite:GPS* are exiftool's own signed-decimal-degrees conversion of the
#: raw EXIF GPS block (which stores degrees/minutes/seconds plus a separate
#: N/S/E/W ref tag) — asking for these directly skips reimplementing that
#: conversion here. Absent on the large majority of photos (most cameras and
#: re-saved/edited exports carry no GPS block at all), same as camera make/model.
#:
#: Three date sources, tried in order by _pick_date_taken() below:
#: EXIF:DateTimeOriginal (true capture time, photos) -> EXIF:CreateDate
#: (usually identical for a straight digital capture; can be the only one
#: present on an edited/re-exported copy) -> QuickTime:CreateDate (videos;
#: technically UTC per the QuickTime spec, while the EXIF tags are naive/
#: local — this project doesn't track timezone anywhere else either, so that
#: distinction is accepted rather than half-fixed for one field only).
_TAGS = ["File:ImageWidth", "File:ImageHeight",
         "EXIF:DateTimeOriginal", "EXIF:CreateDate", "QuickTime:CreateDate",
         "EXIF:Make", "EXIF:Model",
         "Composite:GPSLatitude", "Composite:GPSLongitude",
         "Composite:Duration"]


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


def _plausible(epoch: Optional[float]) -> bool:
    if epoch is None:
        return False
    min_epoch = datetime(_MIN_PLAUSIBLE_YEAR, 1, 1).timestamp()
    # +1 day tolerates the file's own local-time date landing just past
    # "now" in UTC (or vice versa) at a day boundary — not a real clock
    # problem, just the naive/no-timezone parsing this whole module accepts.
    return min_epoch <= epoch <= time.time() + 86400


def _pick_date_taken(raw: dict) -> Optional[float]:
    """First plausible date, tried in the order documented on _TAGS above.
    A camera with a dead clock battery can produce a DateTimeOriginal like
    1970 or 2002 — plausible-but-wrong isn't something a fallback chain can
    detect, but implausible-and-wrong (the common failure mode) at least
    gets skipped in favor of the next source instead of landing in the
    library four decades off.
    """
    for tag in ("EXIF:DateTimeOriginal", "EXIF:CreateDate", "QuickTime:CreateDate"):
        epoch = _parse_date(raw.get(tag))
        if _plausible(epoch):
            return epoch
    return None


def _parse_duration(value) -> Optional[float]:
    """Composite:Duration usually comes back as a plain number of seconds,
    but some containers/exiftool versions print it formatted (e.g. "12.3 s"
    or "0:00:12") regardless — best-effort parse, None on anything unexpected
    rather than raising."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith(" s"):
        text = text[:-2]
    try:
        return float(text)
    except ValueError:
        pass
    if ":" in text:
        parts = text.split(":")
        try:
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + float(part)
            return seconds
        except ValueError:
            return None
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
            # -n: numeric mode. Without it, exiftool print-converts several
            # of the tags above for human display (most visibly Duration,
            # which would otherwise come back as "0:00:12" instead of a
            # plain number of seconds) — GPS/date tags are unaffected either
            # way, exiftool already normalizes those regardless of -n.
            results = helper.get_tags([tmp.name], tags=_TAGS, params=["-n"])
        except Exception:
            return {}

    if not results:
        return {}
    raw = results[0]
    return {
        "width": raw.get("File:ImageWidth"),
        "height": raw.get("File:ImageHeight"),
        "date_taken": _pick_date_taken(raw),
        "camera_make": raw.get("EXIF:Make"),
        "camera_model": raw.get("EXIF:Model"),
        "latitude": raw.get("Composite:GPSLatitude"),
        "longitude": raw.get("Composite:GPSLongitude"),
        "duration_seconds": _parse_duration(raw.get("Composite:Duration")),
    }
