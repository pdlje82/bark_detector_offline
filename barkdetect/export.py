"""Export SQLite contents to JSON for the frontend. Always derived, never edited."""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .coverage import compute_coverage
from .store import Store

log = logging.getLogger(__name__)

# Bump when the results.json shape changes in a way the frontend must handle.
SCHEMA_VERSION = 2

# Labels that are not an individual dog (kept out of per-dog counts).
NON_DOG_LABELS = {"unsure", "multiple", "not_a_dog"}


def _dbfs(raw: float) -> float:
    """Linear amplitude (0..1) -> dBFS, floored at -120."""
    return round(20 * math.log10(raw), 1) if raw and raw > 0 else -120.0


def _is_night(local_dt: datetime, night_start: int, night_end: int) -> bool:
    """Return True if `local_dt`'s hour falls in the night window (may cross midnight)."""
    h = local_dt.hour
    if night_start <= night_end:
        return night_start <= h < night_end
    return h >= night_start or h < night_end  # window crosses midnight


def build_export(cfg, store: Store) -> dict:
    """Build the full results dict (events, coverage, gaps, summaries, provenance)."""
    tz = ZoneInfo(cfg.timezone)
    recordings = store.all_recordings()
    events = store.all_events()

    coverage, gaps = compute_coverage(recordings, cfg.coverage.merge_gap_seconds)

    # Reference loudness for the 0..1 relative intensity. "global" = loudest bark
    # across all recordings; "per_file" = loudest bark within each recording.
    raws = [e["intensity_raw"] for e in events if e["intensity_raw"] is not None]
    global_max = max(raws) if raws else None
    file_max = defaultdict(float)
    for e in events:
        if e["intensity_raw"] is not None:
            file_max[e["recording_id"]] = max(file_max[e["recording_id"]],
                                              e["intensity_raw"])
    per_file = cfg.intensity.scope != "global"

    event_list = []
    daily = defaultdict(lambda: {"count": 0, "total_bark_seconds": 0.0,
                                 "night_count": 0, "by_dog": defaultdict(int)})
    for e in events:
        local_start = datetime.fromisoformat(e["abs_start_utc"]).astimezone(tz)
        night = _is_night(local_start, cfg.coverage.night_start_hour,
                          cfg.coverage.night_end_hour)
        raw = e["intensity_raw"]
        ref = file_max[e["recording_id"]] if per_file else global_max
        rel = round(raw / ref, 4) if (raw is not None and ref) else None
        dog_label = e["dog_label"]
        event_list.append({
            "id": e["id"],
            "key": e["event_key"],
            "recording": e["original_filename"],
            "abs_start_utc": e["abs_start_utc"],
            "abs_start_local": local_start.isoformat(),
            "abs_end_utc": e["abs_end_utc"],
            "duration_sec": e["duration_sec"],
            "peak_conf": e["peak_conf"],
            "mean_conf": e["mean_conf"],
            "class": e["top_class"],
            "night": night,
            "intensity_relative": rel,
            "intensity_dbfs": _dbfs(raw) if raw is not None else None,
            "dog_label": dog_label,
            "dog_confidence": e["dog_confidence"],
            "dog_label_source": e["dog_label_source"],
            "snippet_url": f"snippets/{e['snippet_path']}" if e["snippet_path"] else None,
        })
        day = local_start.date().isoformat()
        d = daily[day]
        d["count"] += 1
        d["total_bark_seconds"] += e["duration_sec"]
        if night:
            d["night_count"] += 1
        if dog_label and dog_label not in NON_DOG_LABELS:
            d["by_dog"][dog_label] += 1

    daily_summary = [
        {"date": day,
         "count": vals["count"],
         "total_bark_seconds": round(vals["total_bark_seconds"], 1),
         "night_count": vals["night_count"],
         "by_dog": dict(vals["by_dog"])}
        for day, vals in sorted(daily.items())
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": cfg.timezone,
        "dogs": list(cfg.identification.dogs),   # roster for the labeling dropdown
        "parameters": cfg.params_snapshot(),   # current config used for this export
        "recording_count": len(recordings),
        "event_count": len(event_list),
        "recordings": [{
            "original_filename": r["original_filename"],
            "sha256": r["sha256"],
            "start_utc": r["start_utc"],
            "start_local": r["start_local"],
            "duration_sec": r["duration_sec"],
            "timestamp_source": r["timestamp_source"],
            "processed_at": r["processed_at"],
            "model_name": r["model_name"],
            "model_version": r["model_version"],
            # parameters that actually produced this recording's events
            "parameters": json.loads(r["parameters_json"]) if r["parameters_json"] else None,
        } for r in recordings],
        "coverage": coverage,
        "gaps": gaps,
        "daily_summary": daily_summary,
        "events": event_list,
    }


def export(cfg, store: Store) -> Path:
    """Write the results JSON into the export dir and return its path."""
    data = build_export(cfg, store)
    export_dir = cfg.path("export_dir")
    export_dir.mkdir(parents=True, exist_ok=True)
    out = export_dir / cfg.export.filename
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("  wrote %s  (%d events, %d gaps)",
             out, data["event_count"], len(data["gaps"]))
    return out
