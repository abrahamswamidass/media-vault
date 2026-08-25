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
