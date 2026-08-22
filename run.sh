#!/usr/bin/env bash
# Convenience wrapper (Linux/macOS). Usage:  ./run.sh nas list --root /data/nas --prefix Photos
set -euo pipefail
docker compose run --rm agent python mediavault.py "$@"
