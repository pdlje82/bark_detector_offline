"""Ingest MP3s from the SD card: hash, derive timestamps, copy to archive.

The recording start time is taken from the SD card's filesystem creation time
(FAT stores this as local wall-clock; on Windows os.stat().st_ctime is the
creation time). This is why ingest must read from the card directly — copying
elsewhere first would destroy the original timestamp.

All behavior is driven by config (run.source, ingest.*). No hardcoded params.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .audio import ffprobe_duration
from .store import Store

log = logging.getLogger(__name__)


def sha256_file(path: str | Path, chunk: int) -> str:
    """Return the SHA-256 hex digest of a file, read in `chunk`-byte blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _iso_utc(ts: float) -> str:
    """Format an epoch timestamp as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _local_dt(ts: float, tz: str) -> datetime:
    """Convert an epoch timestamp to a timezone-aware datetime in `tz`."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(tz))


def find_recordings(source: str | Path, extensions, exclude_dirs=()) -> list[Path]:
    """Recursively find recordings under `source` matching `extensions`.

    Files under any `exclude_dirs` (the pipeline's own archive/snippets/export
    output) are skipped, so the pipeline never re-ingests its own products when
    those dirs live inside the source.
    """
    exts = {e.lower() for e in extensions}
    source = Path(source)
    excludes = [Path(d).resolve() for d in exclude_dirs if d]

    def is_excluded(p: Path) -> bool:
        """True if `p` is one of the excluded dirs or lives inside one."""
        rp = p.resolve()
        return any(rp == ex or ex in rp.parents for ex in excludes)

    return sorted(p for p in source.rglob("*")
                  if p.suffix.lower() in exts and not is_excluded(p))


def ingest(cfg, store: Store) -> dict:
    """Copy new recordings into the archive and register them. Idempotent."""
    ic = cfg.ingest
    source = cfg.run.source
    archive_dir = cfg.path("archive_dir")
    archive_dir.mkdir(parents=True, exist_ok=True)
    # Never re-ingest the pipeline's own output (archive/snippets/export),
    # which may live inside the source dir.
    managed = [archive_dir, cfg.path("snippets_dir"), cfg.path("export_dir")]

    added, skipped = 0, 0
    for src in find_recordings(source, ic.file_extensions, exclude_dirs=managed):
        sha = sha256_file(src, ic.hash_chunk_bytes)
        if store.has_hash(sha):
            skipped += 1
            continue

        st = os.stat(src)                      # read timestamps from the CARD
        try:
            dur = ffprobe_duration(src)
        except RuntimeError as e:
            log.warning("  skipping %s: %s", src.name, e)
            continue

        # Integrity check: the file's modified time should be ~ start + duration.
        # A large drift means a wrong recorder clock or timestamps lost on copy.
        drift = st.st_mtime - (st.st_ctime + dur)
        if abs(drift) > ic.clock_drift_warn_seconds:
            log.warning(
                "  %s: clock-drift %+.0fs — file mtime %s vs expected end %s "
                "(start %s + dur %.0fs). Verify the recorder clock / that "
                "timestamps were preserved.",
                src.name, drift,
                _local_dt(st.st_mtime, cfg.timezone).isoformat(),
                _local_dt(st.st_ctime + dur, cfg.timezone).isoformat(),
                _local_dt(st.st_ctime, cfg.timezone).isoformat(), dur)

        start_local = _local_dt(st.st_ctime, cfg.timezone)
        dest_name = ic.archive_name_template.format(
            start=start_local.strftime("%y%m%d_%H%M"),
            hash=sha[:ic.hash_prefix_len], name=src.name)
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
            "start_local": start_local.isoformat(),
            "timezone": cfg.timezone,
            "timestamp_source": ic.timestamp_source_label,
            "mtime_utc": _iso_utc(st.st_mtime),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        })
        added += 1
        log.info("  ingested %s -> %s  (%.2fh)  start=%s",
                 src.name, dest_name, dur / 3600, start_local.isoformat())

    log.info("  %d added, %d already known.", added, skipped)
    return {"added": added, "skipped": skipped}
