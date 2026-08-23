@echo off
REM Convenience wrapper (Windows). Usage:  run.bat nas list --root /data/nas --prefix Photos
cd /d "%~dp0agent" && docker compose run --rm agent python -m mediavault.cli %*
