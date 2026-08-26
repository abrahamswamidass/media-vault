"""
CLI-level test for `dedup --max-groups`.

Added after a real gap: --limit was assumed to cap how many groups --commit
actually archives, but it only ever trimmed the printed preview list —
--max-groups is the flag that caps the archived batch. Verified here through
the actual CLI entry point (main()), not just at the Action layer, since
that's exactly the layer where the gap went unnoticed.
"""
from __future__ import annotations

from mediavault.catalog import Catalog
from mediavault.cli import main


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_max_groups_caps_how_many_groups_get_archived(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    for i in range(3):
        _write(nas, f"g{i}_a.jpg", f"group{i}".encode() * 10000)
        _write(nas, f"g{i}_b.jpg", f"group{i}".encode() * 10000)
    db = str(tmp_path / "cat.sqlite")

    assert main(["index", "nas", "--root", str(nas), "--db", db, "--quiet"]) == 0

    with Catalog(db) as catalog:
        assert len(catalog.duplicate_groups("nas")) == 3

    assert main(["dedup", "nas", "--root", str(nas), "--db", db,
                "--max-groups", "1", "--commit",
                "--log-dir", str(tmp_path / "actions")]) == 0

    with Catalog(db) as catalog:
        # Only one group actually archived...
        assert catalog.count("nas", state="archived") == 1
        # ...so two groups still have their second (redundant) copy active.
        assert len(catalog.duplicate_groups("nas")) == 2


def test_limit_only_trims_the_preview_not_the_archived_batch(tmp_path, capsys):
    """The gap this all started from: --limit must never silently cap what
    --commit actually does, only what gets printed."""
    nas = tmp_path / "nas"
    nas.mkdir()
    for i in range(3):
        _write(nas, f"g{i}_a.jpg", f"group{i}".encode() * 10000)
        _write(nas, f"g{i}_b.jpg", f"group{i}".encode() * 10000)
    db = str(tmp_path / "cat.sqlite")

    main(["index", "nas", "--root", str(nas), "--db", db, "--quiet"])
    capsys.readouterr()

    main(["dedup", "nas", "--root", str(nas), "--db", db,
         "--limit", "1", "--commit", "--log-dir", str(tmp_path / "actions")])

    with Catalog(db) as catalog:
        # All 3 groups archived despite --limit 1 — limit only affects display.
        assert catalog.count("nas", state="archived") == 3
