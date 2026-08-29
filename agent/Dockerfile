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
# curl, unzip             -> fetches the face-detection model weights below
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        ffmpeg \
        ca-certificates \
        curl \
        unzip \
        tini \
    && rm -rf /var/lib/apt/lists/*

# --- Python deps (cached layer) --------------------------------------------- #
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Face detection model weights (set FACES_LIVE=1 to use) ----------------- #
# Baked in at build time, same as exiftool/ffmpeg above — the alternative is a
# silent ~280MB download (and a hard dependency on outbound internet access)
# the first time any container ever actually runs FACES_LIVE=1, which looks
# identical to detection quietly finding nothing rather than never having run
# at all. Fetched directly (curl+unzip) rather than by invoking
# insightface/onnxruntime, since this build also runs under QEMU for
# linux/arm64 (see the workflow) — a plain file download is
# architecture-agnostic, running onnxruntime here wouldn't be.
#
# Lands under /root/.insightface: this step runs as root (before the USER
# switch below), matching the --user root the documented `docker run`
# command uses at runtime — HOME has to agree at build and run time for the
# baked-in weights to actually be found. The exact same fixed URL
# insightface's own runtime downloader already uses for the "buffalo_l" model
# pack — a deliberate pin, same idea as the >= floor on the insightface
# package itself in requirements.txt; bumping the model later means changing
# this URL on purpose, not picking up a silent upstream change.
RUN mkdir -p /root/.insightface/models/buffalo_l \
    && curl -fsSL https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip \
       -o /tmp/buffalo_l.zip \
    && unzip -q /tmp/buffalo_l.zip -d /root/.insightface/models/buffalo_l \
    && rm /tmp/buffalo_l.zip

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
