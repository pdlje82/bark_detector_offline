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
    snippet_path     TEXT               -- relative to snippets_dir
);

CREATE INDEX IF NOT EXISTS idx_events_recording ON bark_events(recording_id);
CREATE INDEX IF NOT EXISTS idx_events_absstart  ON bark_events(abs_start_utc);
"""


class Store:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- recordings ------------------------------------------------------
    def has_hash(self, sha256: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM recordings WHERE sha256 = ?", (sha256,))
        return cur.fetchone() is not None

    def add_recording(self, rec: dict) -> int:
        cols = ", ".join(rec.keys())
        placeholders = ", ".join("?" for _ in rec)
        cur = self.conn.execute(
            f"INSERT INTO recordings ({cols}) VALUES ({placeholders})",
            tuple(rec.values()),
        )
        self.conn.commit()
        return cur.lastrowid

    def unprocessed_recordings(self) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM recordings WHERE processed_at IS NULL ORDER BY start_utc"
        )
        return cur.fetchall()

    def all_recordings(self) -> list[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM recordings ORDER BY start_utc")
        return cur.fetchall()

    def mark_processed(self, recording_id: int, processed_at: str,
                       model_name: str, model_version: str, parameters_json: str):
        self.conn.execute(
            "UPDATE recordings SET processed_at=?, model_name=?, model_version=?, "
            "parameters_json=? WHERE id=?",
            (processed_at, model_name, model_version, parameters_json, recording_id),
        )
        self.conn.commit()

    # --- events ----------------------------------------------------------
    def clear_events(self, recording_id: int):
        self.conn.execute("DELETE FROM bark_events WHERE recording_id=?", (recording_id,))

    def add_event(self, ev: dict) -> int:
        cols = ", ".join(ev.keys())
        placeholders = ", ".join("?" for _ in ev)
        cur = self.conn.execute(
            f"INSERT INTO bark_events ({cols}) VALUES ({placeholders})",
            tuple(ev.values()),
        )
        return cur.lastrowid

    def commit(self):
        self.conn.commit()

    def all_events(self) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            """SELECT e.*, r.original_filename, r.sha256
               FROM bark_events e JOIN recordings r ON r.id = e.recording_id
               ORDER BY e.abs_start_utc"""
        )
        return cur.fetchall()
