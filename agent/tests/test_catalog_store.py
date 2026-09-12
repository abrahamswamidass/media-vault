"""
Catalog concurrency tests. SQLite's default busy_timeout is 0 — a second
writer fails immediately with "database is locked" instead of waiting its
turn. This matters here specifically because docs/setup.md recommends
running `publish` in a second terminal while `index` is still going, which
is exactly two writers overlapping on the same catalog file.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from mediavault.catalog.store import Catalog


def test_busy_timeout_lets_a_second_writer_wait_instead_of_failing(tmp_path):
    db_path = str(tmp_path / "cat.sqlite")
    writer_a = Catalog(db_path)

    # Acquire and hold the write lock on connection A, simulating a slow
    # in-progress index commit.
    writer_a.conn.execute("BEGIN IMMEDIATE")
    writer_a.conn.execute(
        "INSERT INTO items (source, item_id, name, indexed_at) "
        "VALUES ('t', 'a', 'a', 'now')"
    )

    result: dict = {}

    def try_write_b():
        # Each real docker exec is a separate process with its own connection —
        # a thread with its own Catalog() here is the closest single-process
        # analogue, and sqlite3 connections are thread-affine besides.
        writer_b = Catalog(db_path)
        try:
            writer_b.conn.execute(
                "INSERT INTO items (source, item_id, name, indexed_at) "
                "VALUES ('t', 'b', 'b', 'now')"
            )
            writer_b.conn.commit()
            result["ok"] = True
        except sqlite3.OperationalError as e:
            result["ok"] = False
            result["error"] = str(e)

    t = threading.Thread(target=try_write_b)
    t.start()
    time.sleep(0.3)  # give B time to block on the held lock
    writer_a.conn.commit()  # release it — B should now succeed, not have already failed
    t.join(timeout=5)

    assert result.get("ok") is True, result.get("error")


def _seed(catalog, item_id, *, published=False, skip_reason=None):
    catalog.conn.execute(
        "INSERT INTO items (source, item_id, name, indexed_at, state, published_at, "
        "skip_reason) VALUES ('nas', ?, ?, 'now', 'active', ?, ?)",
        (item_id, item_id, "now" if (published or skip_reason) else None, skip_reason),
    )
    catalog.conn.commit()


def test_publish_stats_reads_published_and_skipped_from_one_query(tmp_path):
    """Regression: computing these as two separate calls (published_count()
    then skipped_count(), or vice versa) lets a concurrently-running
    publish batch make the derived "published" number appear to dip
    between two reads -- each call sees the table at a slightly different
    instant, even though no item is ever actually un-published. One query
    means both numbers always come from the exact same instant."""
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "a.jpg", published=True)
        _seed(catalog, "b.jpg", published=True)
        _seed(catalog, "sidecar.json", skip_reason="not a recognized photo/video extension")
        _seed(catalog, "c.jpg")  # not yet published at all

        published, skipped = catalog.publish_stats("nas")

        assert published == 2
        assert skipped == 1
        # The two individual methods must still agree with the combined read.
        assert catalog.published_count("nas") == published + skipped
        assert catalog.skipped_count("nas") == skipped


def test_mark_published_clears_a_stale_skip_reason(tmp_path):
    """Regression: an item previously skipped, then made eligible again by
    a plain `unpublish` (not --skipped-only, which clears published_at for
    the whole source without touching skip_reason), and then genuinely
    published on a later run -- must not keep reporting as "skipped"
    forever. Nothing else ever clears skip_reason once set, so
    mark_published() has to."""
    with Catalog(str(tmp_path / "cat.sqlite")) as catalog:
        _seed(catalog, "a.jpg", skip_reason="cannot identify image file")

        catalog.mark_published("nas", "a.jpg")

        row = catalog.get("nas", "a.jpg")
        assert row["skip_reason"] is None
        assert row["published_at"] is not None
        published, skipped = catalog.publish_stats("nas")
        assert published == 1
        assert skipped == 0
