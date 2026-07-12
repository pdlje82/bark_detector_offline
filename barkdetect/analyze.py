"""Run detection over unprocessed recordings and persist events + snippets.

Also records the exact parameters (model, normalization, detection) that
produced each recording's events, for chain-of-custody / reproducibility.

Progress and timing are logged per file, including a realtime factor
(audio-hours processed per wall-clock hour) so remaining time is predictable.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from time import perf_counter

from . import detect
from .detect import _fmt_hms
from .snippets import extract_snippet, snippet_relpath
from .store import Store

log = logging.getLogger(__name__)


def _abs_iso(start_utc_iso: str, offset_sec: float) -> str:
    base = datetime.fromisoformat(start_utc_iso)
    return (base + timedelta(seconds=offset_sec)).isoformat()


def analyze(cfg, store: Store) -> dict:
    recs = store.unprocessed_recordings()
    if not recs:
        log.info("  no unprocessed recordings")
        return {"recordings": 0, "events": 0}

    t_load = perf_counter()
    log.info("  loading model (%s) on %s ...", cfg.model.name, cfg.model.device)
    model, labels = detect.load_model(
        device=cfg.model.device, checkpoint_path=cfg.model.checkpoint_path)
    dog_idx = detect.resolve_dog_indices(labels, list(cfg.detection.dog_classes))
    dog_names = list(cfg.detection.dog_classes)
    snippets_dir = cfg.path("snippets_dir")
    params_json = json.dumps(cfg.params_snapshot(), ensure_ascii=False)
    log.info("  model ready in %.1fs — %d recordings to process",
             perf_counter() - t_load, len(recs))

    total_events = 0
    total_audio = 0.0
    run_start = perf_counter()
    for n, rec in enumerate(recs, 1):
        dur = rec["duration_sec"]
        log.info("[%d/%d] %s (%s) — starting", n, len(recs),
                 rec["original_filename"], _fmt_hms(dur))
        file_start = perf_counter()

        # --- detection (the slow phase) ---
        t = perf_counter()
        times, scores, best, energy = detect.score_recording(
            rec["archived_path"], cfg, model, dog_idx, duration_sec=dur)
        events = detect.extract_events(times, scores, best, energy, dog_names, cfg)
        log.info("    detect: %.0fs, %d candidate events",
                 perf_counter() - t, len(events))

        # --- snippets ---
        t = perf_counter()
        store.clear_events(rec["id"])          # idempotent re-analysis
        for ev in events:
            rel = snippet_relpath(rec["sha256"], ev["offset_start_sec"],
                                  cfg.snippets.extension)
            extract_snippet(rec["archived_path"], snippets_dir, rel,
                            ev["offset_start_sec"], ev["duration_sec"], cfg)
            store.add_event({
                "recording_id": rec["id"],
                "offset_start_sec": ev["offset_start_sec"],
                "offset_end_sec": ev["offset_end_sec"],
                "abs_start_utc": _abs_iso(rec["start_utc"], ev["offset_start_sec"]),
                "abs_end_utc": _abs_iso(rec["start_utc"], ev["offset_end_sec"]),
                "duration_sec": ev["duration_sec"],
                "peak_conf": ev["peak_conf"],
                "mean_conf": ev["mean_conf"],
                "top_class": ev["top_class"],
                "intensity_raw": ev["intensity_raw"],
                "snippet_path": rel,
            })
        log.info("    snippets: %.1fs (%d clips)", perf_counter() - t, len(events))

        # --- persist processed state ---
        store.mark_processed(
            rec["id"], datetime.now(timezone.utc).isoformat(),
            cfg.model.name, cfg.model.version, params_json)
        store.commit()

        elapsed = perf_counter() - file_start
        rtf = dur / elapsed if elapsed > 0 else float("nan")
        total_events += len(events)
        total_audio += dur
        log.info("[%d/%d] %s done — %d events in %s  (%.1fx realtime)",
                 n, len(recs), rec["original_filename"], len(events),
                 _fmt_hms(elapsed), rtf)

    run_elapsed = perf_counter() - run_start
    log.info("  analyzed %d recordings (%s of audio) in %s  (%.1fx realtime), "
             "%d events total", len(recs), _fmt_hms(total_audio),
             _fmt_hms(run_elapsed),
             total_audio / run_elapsed if run_elapsed > 0 else float("nan"),
             total_events)
    return {"recordings": len(recs), "events": total_events}
