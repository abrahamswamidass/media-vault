@echo off
REM Convenience wrapper (Windows). Usage:  run.bat nas list --root /data/nas --prefix Photos
docker compose run --rm agent python mediavault.py %*
