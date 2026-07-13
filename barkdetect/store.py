"""SQLite persistence — the source of truth for recordings and bark events."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256            TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    archived_path     TEXT NOT NULL,
    file_size         INTEGER NOT NULL,
    duration_sec      REAL NOT NULL,
    sample_rate       INTEGER NOT NULL,
    start_utc         TEXT NOT NULL,      -- ISO 8601, UTC
    start_local       TEXT NOT NULL,      -- ISO 8601, local (config timezone)
    timezone          TEXT NOT NULL,
    timestamp_source  TEXT NOT NULL,      -- how start_utc was derived
    mtime_utc         TEXT,               -- SD-card modified time (cross-check)
    ingested_at       TEXT NOT NULL,
    processed_at      TEXT,               -- NULL until detection has run
    model_name        TEXT,
    model_version     TEXT,
    parameters_json   TEXT                -- params that produced the events (provenance)
);

CREATE TABLE IF NOT EXISTS bark_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id     INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    offset_start_sec REAL NOT NULL,
    offset_end_sec   REAL NOT NULL,
    abs_start_utc    TEXT NOT NULL,
    abs_end_utc      TEXT NOT NULL,
    duration_sec     REAL NOT NULL,
    peak_conf        REAL NOT NULL,
    mean_conf        REAL NOT NULL,
    top_class        TEXT NOT NULL,
    intensity_raw    REAL,              -- raw loudness (linear 0..1); relative derived at export
    snippet_path     TEXT,              -- relative to snippets_dir
    event_key        TEXT,              -- stable id (sha12_offsetms) for label joins
    embedding        TEXT,              -- JSON float array (bark fingerprint) for identification
    dog_label        TEXT,              -- resolved dog (human or predicted)
    dog_confidence   REAL,              -- prediction confidence (null for human labels)
    dog_label_source TEXT               -- 'human' | 'predicted' | null
);

-- Human labels keyed by the stable event_key, so they survive DB rebuilds.
CREATE TABLE IF NOT EXISTS event_labels (
    event_key  TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'human',
    labeled_at TEXT
);

"""

# Indexes are created AFTER column migration (they may reference newly-added columns).
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_recording ON bark_events(recording_id);
CREATE INDEX IF NOT EXISTS idx_events_absstart  ON bark_events(abs_start_utc);
CREATE INDEX IF NOT EXISTS idx_events_key        ON bark_events(event_key);
"""

# Columns added after the initial schema. On an existing DB they are added via
# ALTER TABLE ADD COLUMN (all nullable), so upgrading never requires a rebuild.
MIGRATIONS = {
    "recordings": {"mtime_utc": "TEXT", "parameters_json": "TEXT"},
    "bark_events": {
        "intensity_raw": "REAL", "event_key": "TEXT", "embedding": "TEXT",
        "dog_label": "TEXT", "dog_confidence": "REAL", "dog_label_source": "TEXT",
    },
}


class Store:
    """Thin SQLite wrapper for recordings and their bark events.

    Opens (creating if needed) the database at `db_path`, ensures the schema
    exists, and exposes small query/insert helpers. Usable as a context manager.
    """

    def __init__(self, db_path: str | Path):
        """Open/create the DB at `db_path` and ensure the schema is present."""
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(INDEXES)
        self.conn.commit()

    def _migrate(self):
        """Add any columns missing from an older DB (ALTER TABLE ADD COLUMN)."""
        for table, cols in MIGRATIONS.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, coltype in cols.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")

    def close(self):
        """Close the database connection."""
        self.conn.close()

    def __enter__(self):
        """Enter the context manager, returning this Store."""
        return self

    def __exit__(self, *exc):
        """Close the connection on context-manager exit."""
        self.close()

    # --- recordings ------------------------------------------------------
    def has_hash(self, sha256: str) -> bool:
        """Return True if a recording with this SHA-256 is already stored."""
        cur = self.conn.execute("SELECT 1 FROM recordings WHERE sha256 = ?", (sha256,))
        return cur.fetchone() is not None

    def add_recording(self, rec: dict) -> int:
        """Insert a recording row from a column->value dict; return its id."""
        cols = ", ".join(rec.keys())
        placeholders = ", ".join("?" for _ in rec)
        cur = self.conn.execute(
            f"INSERT INTO recordings ({cols}) VALUES ({placeholders})",
            tuple(rec.values()),
        )
        self.conn.commit()
        return cur.lastrowid

    def unprocessed_recordings(self) -> list[sqlite3.Row]:
        """Return recordings not yet analyzed (processed_at IS NULL), time-ordered."""
        cur = self.conn.execute(
            "SELECT * FROM recordings WHERE processed_at IS NULL ORDER BY start_utc"
        )
        return cur.fetchall()

    def all_recordings(self) -> list[sqlite3.Row]:
        """Return all recordings ordered by recording start time."""
        cur = self.conn.execute("SELECT * FROM recordings ORDER BY start_utc")
        return cur.fetchall()

    def mark_processed(self, recording_id: int, processed_at: str,
                       model_name: str, model_version: str, parameters_json: str):
        """Record that a recording was analyzed, storing model + parameter provenance."""
        self.conn.execute(
            "UPDATE recordings SET processed_at=?, model_name=?, model_version=?, "
            "parameters_json=? WHERE id=?",
            (processed_at, model_name, model_version, parameters_json, recording_id),
        )
        self.conn.commit()

    # --- events ----------------------------------------------------------
    def clear_events(self, recording_id: int):
        """Delete all events for a recording (idempotent re-analysis)."""
        self.conn.execute("DELETE FROM bark_events WHERE recording_id=?", (recording_id,))

    def add_event(self, ev: dict) -> int:
        """Insert a bark-event row from a column->value dict; return its id.

        Not committed here — call commit() after a batch.
        """
        cols = ", ".join(ev.keys())
        placeholders = ", ".join("?" for _ in ev)
        cur = self.conn.execute(
            f"INSERT INTO bark_events ({cols}) VALUES ({placeholders})",
            tuple(ev.values()),
        )
        return cur.lastrowid

    def commit(self):
        """Commit the current transaction."""
        self.conn.commit()

    def all_events(self) -> list[sqlite3.Row]:
        """Return all events joined with their recording's filename + hash, time-ordered."""
        cur = self.conn.execute(
            """SELECT e.*, r.original_filename, r.sha256
               FROM bark_events e JOIN recordings r ON r.id = e.recording_id
               ORDER BY e.abs_start_utc"""
        )
        return cur.fetchall()

    # --- identification --------------------------------------------------
    def upsert_label(self, event_key: str, label: str, source: str, labeled_at: str):
        """Insert or replace a human label for a stable event_key."""
        self.conn.execute(
            "INSERT INTO event_labels (event_key, label, source, labeled_at) "
            "VALUES (?,?,?,?) ON CONFLICT(event_key) DO UPDATE SET "
            "label=excluded.label, source=excluded.source, labeled_at=excluded.labeled_at",
            (event_key, label, source, labeled_at),
        )

    def all_labels(self) -> dict[str, str]:
        """Return {event_key: label} for all stored human labels."""
        cur = self.conn.execute("SELECT event_key, label FROM event_labels")
        return {r["event_key"]: r["label"] for r in cur.fetchall()}

    def events_with_embeddings(self) -> list[sqlite3.Row]:
        """Return events that have an embedding, with their key and embedding."""
        cur = self.conn.execute(
            "SELECT id, event_key, embedding FROM bark_events "
            "WHERE embedding IS NOT NULL"
        )
        return cur.fetchall()

    def set_event_prediction(self, event_id: int, dog_label: str | None,
                             confidence: float | None, source: str | None):
        """Store the resolved dog label + source (+ confidence) on an event."""
        self.conn.execute(
            "UPDATE bark_events SET dog_label=?, dog_confidence=?, dog_label_source=? "
            "WHERE id=?",
            (dog_label, confidence, source, event_id),
        )
