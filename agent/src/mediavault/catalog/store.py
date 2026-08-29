"""
The local catalog — a SQLite index of everything the agent has seen.

Two jobs:

  * **Answer questions without touching the mount.** "How many photos?", "what
    duplicates exist?", "what changed since Tuesday?" all resolve from here.
  * **Survive a crash.** A 1 TB scan takes hours. The `scans` table holds a
    cursor so an interrupted pass resumes from the last committed directory
    rather than starting over.

The catalog is a cache, never a source of truth. If it disagrees with the NAS,
the NAS is right and the next scan corrects it. Deleting the file is always safe.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..ports import FileRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER,
    mtime       REAL,
    mime        TEXT,
    quick_hash  TEXT,
    full_hash   TEXT,
    state       TEXT NOT NULL DEFAULT 'active',
    indexed_at  TEXT NOT NULL,
    published_at TEXT,
    width       INTEGER,
    height      INTEGER,
    date_taken  REAL,
    camera_make TEXT,
    camera_model TEXT,
    latitude    REAL,
    longitude   REAL,
    duration_seconds REAL,
    PRIMARY KEY (source, item_id)
);

-- Dedup always groups within one source, never across. The index reflects that.
CREATE INDEX IF NOT EXISTS idx_items_dedup
    ON items (source, quick_hash) WHERE state = 'active' AND quick_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_items_state ON items (source, state);

CREATE TABLE IF NOT EXISTS scans (
    source      TEXT PRIMARY KEY,
    cursor      TEXT,
    started_at  TEXT,
    updated_at  TEXT,
    items_seen  INTEGER NOT NULL DEFAULT 0,
    complete    INTEGER NOT NULL DEFAULT 0
);

-- A person is a cluster of faces believed to be the same individual.
-- Unnamed (name IS NULL) until labeled — clustering itself never assigns a
-- name, only groups faces together for a person to do that once. Declared
-- before faces (below), which references it.
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    created_at  TEXT NOT NULL
);

-- One row per detected face. Embeddings never leave this local database —
-- only the (opaque) person_id they resolve to gets published to Firestore,
-- see PublishAction. person_id is NULL only transiently, between insert and
-- the clustering step that assigns it; nothing in this project leaves it
-- unset on purpose.
CREATE TABLE IF NOT EXISTS faces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    bbox_x1     REAL NOT NULL,
    bbox_y1     REAL NOT NULL,
    bbox_x2     REAL NOT NULL,
    bbox_y2     REAL NOT NULL,
    score       REAL,
    embedding   BLOB NOT NULL,
    person_id   INTEGER REFERENCES people(id),
    detected_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_faces_item ON faces (source, item_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces (person_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    """SQLite-backed index. One file, no server, safe to delete."""

    def __init__(self, db_path: str = "/data/catalog/catalog.sqlite"):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        # WAL keeps reads working while a long scan writes. SQLite's own
        # default busy_timeout is 0 — a second writer (e.g. `publish`
        # running in another terminal while `index` is still going) fails
        # immediately with "database is locked" instead of briefly waiting
        # its turn, which is what actually happens under normal SQLite
        # concurrency. 30s comfortably covers one commit's worth of wait.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a catalog was first created.

        `CREATE TABLE IF NOT EXISTS` in SCHEMA only shapes a brand-new database —
        an existing one (like a NAS already indexed before `published_at` existed)
        needs its own ALTER TABLE. SQLite has no "ADD COLUMN IF NOT EXISTS", so we
        just swallow the "duplicate column" error on a re-run.
        """
        for ddl in (
            "ALTER TABLE items ADD COLUMN published_at TEXT",
            "ALTER TABLE items ADD COLUMN width INTEGER",
            "ALTER TABLE items ADD COLUMN height INTEGER",
            "ALTER TABLE items ADD COLUMN date_taken REAL",
            "ALTER TABLE items ADD COLUMN camera_make TEXT",
            "ALTER TABLE items ADD COLUMN camera_model TEXT",
            "ALTER TABLE items ADD COLUMN latitude REAL",
            "ALTER TABLE items ADD COLUMN longitude REAL",
            "ALTER TABLE items ADD COLUMN duration_seconds REAL",
        ):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- writing ---------------------------------------------------------- #
    def upsert(self, source: str, record: FileRecord) -> None:
        """Record one item. Re-indexing an unchanged file is a cheap no-op write."""
        self.conn.execute(
            """
            INSERT INTO items (source, item_id, name, size, mtime, mime,
                               quick_hash, state, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            ON CONFLICT(source, item_id) DO UPDATE SET
                name       = excluded.name,
                size       = excluded.size,
                mtime      = excluded.mtime,
                mime       = excluded.mime,
                quick_hash = excluded.quick_hash,
                indexed_at = excluded.indexed_at,
                -- content changed? the old full hash is stale.
                full_hash  = CASE WHEN items.quick_hash IS NOT excluded.quick_hash
                                  THEN NULL ELSE items.full_hash END
            """,
            (source, record.id, record.name, record.size, record.mtime,
             record.mime, record.quick_hash, _now()),
        )

    def set_full_hash(self, source: str, item_id: str, full_hash: str) -> None:
        self.conn.execute(
            "UPDATE items SET full_hash = ? WHERE source = ? AND item_id = ?",
            (full_hash, source, item_id),
        )

    # --- faces / people ----------------------------------------------------#
    def add_person(self) -> int:
        """Start a new, unnamed person — a face didn't match anyone existing
        closely enough (see catalog/people.py's clustering)."""
        cur = self.conn.execute(
            "INSERT INTO people (name, created_at) VALUES (NULL, ?)", (_now(),))
        self.conn.commit()
        return cur.lastrowid

    def set_person_name(self, person_id: int, name: str) -> None:
        self.conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
        self.conn.commit()

    def add_face(self, source: str, item_id: str, bbox: tuple[float, float, float, float],
                score: Optional[float], embedding: bytes, person_id: int) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO faces (source, item_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                               score, embedding, person_id, detected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, item_id, *bbox, score, embedding, person_id, _now()),
        )
        self.conn.commit()
        return cur.lastrowid

    def person_centroids(self) -> list[sqlite3.Row]:
        """One representative embedding per known person — their first-ever
        detected face, used as the comparison point when matching a new one.
        Simple and deterministic rather than averaging embeddings across
        every face a person has, which would need re-averaging on every
        insert for a benefit that hasn't proven necessary."""
        return self.conn.execute(
            """
            SELECT f.person_id AS person_id, f.embedding AS embedding
            FROM faces f
            JOIN (SELECT person_id, MIN(id) AS first_id FROM faces
                  WHERE person_id IS NOT NULL GROUP BY person_id) first
              ON f.id = first.first_id
            """
        ).fetchall()

    def faces_for_item(self, source: str, item_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM faces WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchall()

    def list_people(self) -> list[sqlite3.Row]:
        """Every person, with how many faces/distinct items they appear in
        and one sample item to look at (their first-ever detected face) —
        enough to sanity-check clustering quality from the CLI without
        needing the eventual web "People" tab."""
        return self.conn.execute(
            """
            SELECT p.id AS id, p.name AS name, p.created_at AS created_at,
                   COUNT(f.id) AS face_count,
                   COUNT(DISTINCT f.item_id) AS item_count,
                   MIN(f.item_id) AS sample_item_id
            FROM people p JOIN faces f ON f.person_id = p.id
            GROUP BY p.id
            ORDER BY face_count DESC
            """
        ).fetchall()

    def mark_archived(self, source: str, item_id: str) -> None:
        """Flag an item as archived. The row stays so the change is visible."""
        self.conn.execute(
            "UPDATE items SET state = 'archived' WHERE source = ? AND item_id = ?",
            (source, item_id),
        )

    def mark_published(self, source: str, item_id: str) -> None:
        """Flag an item as published (thumbnail + facts pushed). Re-run-proof:
        a later publish pass only looks at rows still missing this."""
        self.conn.execute(
            "UPDATE items SET published_at = ? WHERE source = ? AND item_id = ?",
            (_now(), source, item_id),
        )

    def set_exif(self, source: str, item_id: str, exif: dict) -> None:
        """Record EXIF fields pulled from the file (see metadata.py). Missing
        fields (most photos have partial or no EXIF) just store NULL."""
        self.conn.execute(
            """
            UPDATE items SET width = ?, height = ?, date_taken = ?,
                             camera_make = ?, camera_model = ?,
                             latitude = ?, longitude = ?, duration_seconds = ?
            WHERE source = ? AND item_id = ?
            """,
            (exif.get("width"), exif.get("height"), exif.get("date_taken"),
             exif.get("camera_make"), exif.get("camera_model"),
             exif.get("latitude"), exif.get("longitude"),
             exif.get("duration_seconds"), source, item_id),
        )

    def reset(self, source: str | None = None) -> dict:
        """Wipe catalog rows for one source, or everything if source is None.

        Testing/dev convenience — a re-index rebuilds this from scratch, so it's
        always safe to throw away. Never touches the NAS or any cloud store:
        thumbnails/facts already published stay right where they are, and are
        content-addressed, so re-publishing after a re-index is a no-op rather
        than a re-upload.
        """
        with self.transaction() as c:
            if source:
                items = c.execute("DELETE FROM items WHERE source = ?", (source,)).rowcount
                scans = c.execute("DELETE FROM scans WHERE source = ?", (source,)).rowcount
            else:
                items = c.execute("DELETE FROM items").rowcount
                scans = c.execute("DELETE FROM scans").rowcount
        return {"items_deleted": items, "scans_deleted": scans}

    # --- reading ---------------------------------------------------------- #
    def get(self, source: str, item_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE source = ? AND item_id = ?", (source, item_id)
        ).fetchone()

    def count(self, source: str | None = None, state: str = "active") -> int:
        if source:
            return self.conn.execute(
                "SELECT COUNT(*) FROM items WHERE source = ? AND state = ?",
                (source, state)).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE state = ?", (state,)).fetchone()[0]

    def published_count(self, source: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM items WHERE source = ? AND state = 'active' "
            "AND published_at IS NOT NULL", (source,)).fetchone()[0]

    def sources(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT source FROM items ORDER BY source")]

    def unpublished(self, source: str, limit: Optional[int] = None,
                    force: bool = False) -> list[sqlite3.Row]:
        """Active, hashed items with no thumbnail/facts pushed yet, oldest-indexed first.

        A hash is required — that's what a thumbnail is content-addressed by —
        so an item still mid-scan (no quick_hash yet) is correctly skipped.

        `force=True` also includes already-published items — for backfilling a
        newly-added fact field (e.g. GPS) onto a library that was published
        before that field existed, without a full reset + re-index. Thumbnails
        stay untouched either way: they're content-addressed and unchanged, so
        re-deriving one is wasted work regardless of `force`.
        """
        sql = (
            "SELECT * FROM items WHERE source = ? AND state = 'active' "
            "AND quick_hash IS NOT NULL "
        )
        if not force:
            sql += "AND published_at IS NULL "
        sql += "ORDER BY indexed_at, item_id"
        params: tuple = (source,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (source, limit)
        return self.conn.execute(sql, params).fetchall()

    def duplicate_groups(self, source: str, min_size: int = 1) -> list[list[sqlite3.Row]]:
        """Active items in one source sharing a quick_hash, grouped.

        Scoped to a single source on purpose. NAS and Drive holding the same photo
        is this system working as designed — Drive is the curated cloud copy — so a
        cross-source comparison would propose exactly the deletions we must never make.

        `min_size` skips trivially small files, where a shared head/tail fingerprint
        is more likely to be a coincidence than a real duplicate.
        """
        rows = self.conn.execute(
            """
            SELECT * FROM items
            WHERE source = ? AND state = 'active'
              AND quick_hash IS NOT NULL
              AND size >= ?
              AND quick_hash IN (
                  SELECT quick_hash FROM items
                  WHERE source = ? AND state = 'active' AND quick_hash IS NOT NULL
                    AND size >= ?
                  GROUP BY quick_hash HAVING COUNT(*) > 1
              )
            ORDER BY quick_hash, mtime, item_id
            """,
            (source, min_size, source, min_size),
        ).fetchall()

        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            groups.setdefault(row["quick_hash"], []).append(row)
        return list(groups.values())

    def wasted_bytes(self, source: str) -> int:
        """How much space the redundant copies occupy — total minus one per group."""
        return sum(
            g[0]["size"] * (len(g) - 1)
            for g in self.duplicate_groups(source)
            if g and g[0]["size"]
        )

    # --- scan checkpointing ----------------------------------------------- #
    def begin_scan(self, source: str, resume: bool = True) -> str:
        """Start or resume a scan. Returns the cursor to resume from ('' if fresh)."""
        row = self.conn.execute(
            "SELECT cursor, complete FROM scans WHERE source = ?", (source,)).fetchone()
        if resume and row and not row["complete"]:
            return row["cursor"] or ""
        with self.transaction() as c:
            c.execute(
                """
                INSERT INTO scans (source, cursor, started_at, updated_at, items_seen, complete)
                VALUES (?, '', ?, ?, 0, 0)
                ON CONFLICT(source) DO UPDATE SET
                    cursor = '', started_at = excluded.started_at,
                    updated_at = excluded.updated_at, items_seen = 0, complete = 0
                """,
                (source, _now(), _now()),
            )
        return ""

    def checkpoint(self, source: str, cursor: str, items_seen: int) -> None:
        """Commit progress. Called per directory, so a crash costs one directory."""
        with self.transaction() as c:
            c.execute(
                "UPDATE scans SET cursor = ?, updated_at = ?, items_seen = ? WHERE source = ?",
                (cursor, _now(), items_seen, source),
            )

    def finish_scan(self, source: str) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE scans SET complete = 1, cursor = '', updated_at = ? WHERE source = ?",
                (_now(), source),
            )

    def scan_state(self, source: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scans WHERE source = ?", (source,)).fetchone()
