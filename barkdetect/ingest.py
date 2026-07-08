"""Ingest MP3s from the SD card: hash, derive timestamps, copy to archive.

The recording start time is taken from the SD card's filesystem creation time
(FAT stores this as local wall-clock; on Windows os.stat().st_ctime is the
creation time). This is why ingest must read from the card directly — copying
elsewhere first would destroy the original timestamp.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .audio import ffprobe_duration
from .store import Store

TIMESTAMP_SOURCE = "sdcard_ctime"


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _iso_local(ts: float, tz: str) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(tz)).isoformat()


def find_mp3s(source: str | Path) -> list[Path]:
    source = Path(source)
    return sorted(p for p in source.rglob("*") if p.suffix.lower() == ".mp3")


def ingest(source: str | Path, cfg, store: Store) -> dict:
    """Copy new recordings into the archive and register them. Idempotent."""
    archive_dir = cfg.path("archive_dir")
    archive_dir.mkdir(parents=True, exist_ok=True)

    added, skipped = 0, 0
    for src in find_mp3s(source):
        sha = sha256_file(src)
        if store.has_hash(sha):
            skipped += 1
            continue

        st = os.stat(src)                      # read timestamps from the CARD
        dur = ffprobe_duration(src)

        dest_name = f"{sha[:12]}_{src.name}"
        dest = archive_dir / dest_name
        shutil.copy2(src, dest)                # copy2 preserves mtime

        store.add_recording({
            "sha256": sha,
            "original_filename": src.name,
            "archived_path": str(dest),
            "file_size": st.st_size,
            "duration_sec": dur,
            "sample_rate": cfg.audio.sample_rate,
            "start_utc": _iso_utc(st.st_ctime),
            "start_local": _iso_local(st.st_ctime, cfg.timezone),
            "timezone": cfg.timezone,
            "timestamp_source": TIMESTAMP_SOURCE,
            "mtime_utc": _iso_utc(st.st_mtime),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        })
        added += 1
        print(f"  ingested {src.name}  ({dur/3600:.2f}h)  start={_iso_local(st.st_ctime, cfg.timezone)}")

    return {"added": added, "skipped": skipped}
