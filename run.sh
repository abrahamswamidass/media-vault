#!/usr/bin/env bash
# Run from the repo root; the agent's compose file lives in agent/.
# Convenience wrapper (Linux/macOS). Usage:  ./run.sh nas list --root /data/nas --prefix Photos
set -euo pipefail
cd "$(dirname "$0")/agent" && docker compose run --rm agent python -m mediavault.cli "$@"
