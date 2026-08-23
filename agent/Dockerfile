# syntax=docker/dockerfile:1
###############################################################################
# Media Vault — Local Agent image
#
# Bakes in the two media tools the indexer depends on so metadata extraction
# works identically on Windows, macOS, and Linux hosts:
#   - exiftool  (photo/video/doc metadata — the "gold standard")
#   - ffmpeg    (provides ffprobe for video duration/codec/resolution)
#
# The image is a portable UNIT: the same container runs on Docker Desktop
# (Windows/macOS) today and on a Synology / NUC / Linux box tomorrow — no code
# changes, just move the compose file.
###############################################################################
FROM python:3.12-slim

# --- OS-level media tooling ------------------------------------------------- #
# libimage-exiftool-perl -> the `exiftool` binary
# ffmpeg                 -> the `ffprobe` binary
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        ffmpeg \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

# --- Python deps (cached layer) --------------------------------------------- #
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- App code --------------------------------------------------------------- #
# src/ layout: the package lives at /app/src/mediavault, and PYTHONPATH below
# puts it on the import path without an install step.
COPY pyproject.toml .
COPY src/ /app/src/

# Non-root user for safety; can still read the mounted NAS (read-only) and
# write only to the trash/staging paths you explicitly mount as writable.
RUN useradd -m -u 1000 vault && chown -R vault:vault /app
USER vault

# Where the mounted NAS share and local catalog live INSIDE the container.
# Override via .env / compose if you like; code uses POSIX paths only.
ENV NAS_ROOT=/data/nas \
    NAS_TRASH=/data/nas/_trash \
    ARCHIVE_ROOT=/data/archives \
    AMAZON_STAGING=/data/amazon_staging \
    CATALOG_DB=/data/catalog/catalog.sqlite \
    ACTION_LOG=/data/catalog/actions \
    BLOB_CACHE=/data/catalog/blobs \
    DRIVE_LIVE=0 \
    GCS_LIVE=0 \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# tini = clean signal handling / zombie reaping for a long-lived agent.
ENTRYPOINT ["tini", "--"]

# Default: print the CLI help so `docker run <image>` is self-documenting.
# Override with any command, e.g.:
#   docker compose run --rm agent nas list --root /data/nas --prefix Photos
CMD ["python", "-m", "mediavault.cli", "--help"]
