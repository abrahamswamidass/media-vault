"""
Periodic background tasks inside `process-intents --watch`: the weekly
cold-archive push and the weekly re-index, both opt-in and both built on
the same generic `schedules` table mechanism.

Verifies schedule state survives via the catalog (not an in-memory timer
-- see _schedule_due's callers for why that matters given how often this
project's container gets recreated), and that a GCS_LIVE=1-with-no-bucket
misconfiguration is skipped cleanly rather than crashing the whole loop.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from mediavault.catalog import Catalog
from mediavault.catalog import scanner
from mediavault.cli import (
    _maybe_run_scheduled_cold_archive,
    _maybe_run_scheduled_index,
)
from mediavault.connectors.nas import NASConnector


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def catalog(tmp_path):
    with Catalog(str(tmp_path / "cat.sqlite")) as c:
        yield c


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.delenv("NAS_MODE", raising=False)  # force the mount-based connector
    nas = tmp_path / "nas"
    _write(nas, "img.jpg", b"pretend-jpeg" * 100)
    return argparse.Namespace(root=str(nas), trash=None, permanent=False,
                              coldstore_dir=str(tmp_path / "coldstore"))


# --------------------------------------------------------------------------- #
# cold-archive schedule
# --------------------------------------------------------------------------- #
def test_cold_archive_disabled_by_default_does_nothing(args, catalog, monkeypatch, capsys):
    monkeypatch.delenv("COLD_ARCHIVE_SCHEDULE", raising=False)

    _maybe_run_scheduled_cold_archive(args, catalog)

    assert catalog.get_last_scheduled_run("cold_archive_nas") is None
    assert capsys.readouterr().out == ""


def test_cold_archive_gcs_live_without_bucket_skips_instead_of_crashing(
        args, catalog, monkeypatch, capsys):
    """Regression guard: _coldstore_for() raises SystemExit in this exact
    configuration -- this must be caught before ever calling it, not after,
    or the whole watch loop dies instead of just skipping this check."""
    monkeypatch.setenv("COLD_ARCHIVE_SCHEDULE", "1")
    monkeypatch.setenv("GCS_LIVE", "1")
    monkeypatch.delenv("COLD_STORAGE_BUCKET", raising=False)

    _maybe_run_scheduled_cold_archive(args, catalog)  # must not raise

    assert catalog.get_last_scheduled_run("cold_archive_nas") is None
    assert "skipping" in capsys.readouterr().out


def test_cold_archive_runs_on_first_call_and_marks_the_schedule(args, catalog, monkeypatch):
    monkeypatch.setenv("COLD_ARCHIVE_SCHEDULE", "1")
    monkeypatch.delenv("GCS_LIVE", raising=False)  # local-folder fallback
    monkeypatch.setenv("COLD_ARCHIVE_SOURCE", "nas")
    scanner.scan(NASConnector(args.root), catalog, source="nas")

    _maybe_run_scheduled_cold_archive(args, catalog)

    assert catalog.get_last_scheduled_run("cold_archive_nas") is not None
    assert catalog.cold_archived_count("nas") == 1


def test_cold_archive_skips_when_run_recently(args, catalog, monkeypatch):
    monkeypatch.setenv("COLD_ARCHIVE_SCHEDULE", "1")
    monkeypatch.delenv("GCS_LIVE", raising=False)
    scanner.scan(NASConnector(args.root), catalog, source="nas")
    catalog.mark_scheduled_run("cold_archive_nas")

    _maybe_run_scheduled_cold_archive(args, catalog)

    assert catalog.cold_archived_count("nas") == 0, "ran again despite being inside the interval"


def test_cold_archive_runs_again_once_the_interval_has_passed(args, catalog, monkeypatch):
    monkeypatch.setenv("COLD_ARCHIVE_SCHEDULE", "1")
    monkeypatch.setenv("COLD_ARCHIVE_INTERVAL_DAYS", "7")
    monkeypatch.delenv("GCS_LIVE", raising=False)
    scanner.scan(NASConnector(args.root), catalog, source="nas")
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    catalog.conn.execute(
        "INSERT INTO schedules (name, last_run_at) VALUES (?, ?)",
        ("cold_archive_nas", eight_days_ago))
    catalog.conn.commit()

    _maybe_run_scheduled_cold_archive(args, catalog)

    assert catalog.cold_archived_count("nas") == 1


def test_cold_archive_schedule_name_follows_the_configured_source(args, catalog, monkeypatch):
    """A schedule for one source must not read as satisfied by a run
    recorded under a different source's name."""
    monkeypatch.setenv("COLD_ARCHIVE_SCHEDULE", "1")
    monkeypatch.delenv("GCS_LIVE", raising=False)
    monkeypatch.setenv("COLD_ARCHIVE_SOURCE", "nas")
    catalog.mark_scheduled_run("cold_archive_drive")  # a different source's schedule
    scanner.scan(NASConnector(args.root), catalog, source="nas")

    _maybe_run_scheduled_cold_archive(args, catalog)

    assert catalog.get_last_scheduled_run("cold_archive_nas") is not None
    assert catalog.cold_archived_count("nas") == 1


# --------------------------------------------------------------------------- #
# index schedule
# --------------------------------------------------------------------------- #
def test_index_disabled_by_default_does_nothing(args, catalog, monkeypatch, capsys):
    monkeypatch.delenv("INDEX_SCHEDULE", raising=False)

    _maybe_run_scheduled_index(args, catalog)

    assert catalog.get_last_scheduled_run("index_nas") is None
    assert catalog.count("nas") == 0
    assert capsys.readouterr().out == ""


def test_index_runs_on_first_call_and_marks_the_schedule(args, catalog, monkeypatch):
    monkeypatch.setenv("INDEX_SCHEDULE", "1")

    _maybe_run_scheduled_index(args, catalog)

    assert catalog.get_last_scheduled_run("index_nas") is not None
    assert catalog.count("nas") == 1


def test_index_skips_when_run_recently(args, catalog, monkeypatch):
    monkeypatch.setenv("INDEX_SCHEDULE", "1")
    catalog.mark_scheduled_run("index_nas")

    _maybe_run_scheduled_index(args, catalog)

    assert catalog.count("nas") == 0, "ran again despite being inside the interval"


def test_index_runs_again_once_the_interval_has_passed(args, catalog, monkeypatch):
    monkeypatch.setenv("INDEX_SCHEDULE", "1")
    monkeypatch.setenv("INDEX_INTERVAL_DAYS", "7")
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    catalog.conn.execute(
        "INSERT INTO schedules (name, last_run_at) VALUES (?, ?)",
        ("index_nas", eight_days_ago))
    catalog.conn.commit()

    _maybe_run_scheduled_index(args, catalog)

    assert catalog.count("nas") == 1


def test_index_resumes_an_interrupted_scan_instead_of_restarting(args, catalog, monkeypatch):
    """A scheduled run should behave like a plain `index nas` -- resumable,
    not a forced full restart -- since it may itself get interrupted by a
    container recreation mid-scan, same risk a manual run already has."""
    monkeypatch.setenv("INDEX_SCHEDULE", "1")
    catalog.begin_scan("nas", resume=True)
    catalog.checkpoint("nas", "somewhere", 0)  # leaves scan_state incomplete

    _maybe_run_scheduled_index(args, catalog)

    state = catalog.scan_state("nas")
    assert state["complete"] == 1


# --------------------------------------------------------------------------- #
# shared plumbing
# --------------------------------------------------------------------------- #
def test_catalog_schedule_round_trip(catalog):
    assert catalog.get_last_scheduled_run("x") is None
    catalog.mark_scheduled_run("x")
    first = catalog.get_last_scheduled_run("x")
    assert first is not None
    catalog.mark_scheduled_run("x")
    assert catalog.get_last_scheduled_run("x") >= first
