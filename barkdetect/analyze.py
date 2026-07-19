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
from .audio import read_segment
from .detect import _fmt_hms
from .embeddings import embed_samples
from .guard import enforce_guard, segmentation_fingerprint
from .snippets import extract_snippet, snippet_relpath
from .store import Store

log = logging.getLogger(__name__)


def _abs_iso(start_utc_iso: str, offset_sec: float) -> str:
    """Return the ISO-8601 UTC time `offset_sec` after a recording's start."""
    base = datetime.fromisoformat(start_utc_iso)
    return (base + timedelta(seconds=offset_sec)).isoformat()


def analyze(cfg, store: Store) -> dict:
    """Detect barks in all unprocessed recordings; persist events, snippets, provenance.

    Returns a summary dict with the number of recordings and events processed.
    """
    reprocess = getattr(cfg.run, "reprocess", False)
    recs = store.all_recordings() if reprocess else store.unprocessed_recordings()
    if not recs:
        log.info("  no recordings to %s", "reprocess" if reprocess else "process")
        return {"recordings": 0, "events": 0}
    if reprocess:
        log.info("  reprocess mode: re-analyzing all %d recordings", len(recs))

    # Guard human labels against a segmentation change before touching events.
    fingerprint = segmentation_fingerprint(cfg)
    enforce_guard(cfg, store, fingerprint)

    t_load = perf_counter()
    log.info("  loading model (%s) on %s ...", cfg.model.name, cfg.model.device)
    model, labels = detect.load_model(
        device=cfg.model.device, checkpoint_path=cfg.model.checkpoint_path)
    dog_idx = detect.resolve_dog_indices(labels, list(cfg.detection.dog_classes))
    dog_names = list(cfg.detection.dog_classes)
    snippets_dir = cfg.path("snippets_dir")
    params_json = json.dumps(cfg.params_snapshot(), ensure_ascii=False)
    log.info("  model ready in %.1fs - %d recordings to process",
             perf_counter() - t_load, len(recs))

    total_events = 0
    total_audio = 0.0
    run_start = perf_counter()
    for n, rec in enumerate(recs, 1):
        dur = rec["duration_sec"]
        start_local = datetime.fromisoformat(rec["start_local"])
        end_local = start_local + timedelta(seconds=dur)
        # show end date too when the recording crosses midnight
        end_fmt = (end_local.strftime("%Y-%m-%d %H:%M:%S")
                   if end_local.date() != start_local.date()
                   else end_local.strftime("%H:%M:%S"))
        log.info("[%d/%d] %s - recorded %s -> %s  (dur %s)  [tz %s, src %s]",
                 n, len(recs), rec["original_filename"],
                 start_local.strftime("%a %Y-%m-%d %H:%M:%S"), end_fmt,
                 _fmt_hms(dur), rec["timezone"], rec["timestamp_source"])
        file_start = perf_counter()

        # --- detection (the slow phase) ---
        t = perf_counter()
        times, scores, best, energy = detect.score_recording(
            rec["archived_path"], cfg, model, dog_idx, duration_sec=dur)
        events = detect.extract_events(times, scores, best, energy, dog_names, cfg,
                                       audio_path=rec["archived_path"])
        log.info("    detect: %.0fs, %d candidate events",
                 perf_counter() - t, len(events))

        # --- per-event: embedding, optional clip, persist ---
        t = perf_counter()
        store.clear_events(rec["id"])          # idempotent re-analysis
        identify_on = cfg.identification.enabled
        snippets_on = getattr(cfg.snippets, "enabled", True)
        sr = cfg.audio.sample_rate
        # Decode each detection region ONCE and slice per event in memory, so a
        # burst of N onset sub-events costs one decode instead of N ffmpeg spawns.
        region_key = None
        region_seg = None
        region_start = 0.0
        for ev in events:
            # stable id (independent of intensity/dbfs) used to join human labels
            ms = int(round(ev["offset_start_sec"] * 1000))
            event_key = f"{rec['sha256'][:12]}_{ms:09d}"
            rel = None
            if snippets_on:
                event_local = start_local + timedelta(seconds=ev["offset_start_sec"])
                rel = snippet_relpath(cfg, rec["sha256"], event_local,
                                      ev["offset_start_sec"], ev["intensity_raw"])
                extract_snippet(rec["archived_path"], snippets_dir, rel,
                                ev["offset_start_sec"], ev["duration_sec"], cfg)
            embedding = None
            if identify_on:
                r0 = ev.get("region_start_sec", ev["offset_start_sec"])
                r1 = ev.get("region_end_sec", ev["offset_end_sec"])
                key = (round(r0, 3), round(r1, 3))
                if key != region_key:
                    region_seg = read_segment(rec["archived_path"], sr,
                                              r0, max(0.05, r1 - r0))
                    region_key, region_start = key, r0
                s = int((ev["offset_start_sec"] - region_start) * sr)
                e = s + int(ev["duration_sec"] * sr)
                s = max(0, min(s, region_seg.size))
                e = max(s, min(e, region_seg.size))
                vec = embed_samples(region_seg[s:e], cfg)
                embedding = json.dumps([round(float(x), 6) for x in vec])
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
                "event_key": event_key,
                "embedding": embedding,
            })
        log.info("    per-event: %.1fs (%d events, clips=%s)",
                 perf_counter() - t, len(events), "on" if snippets_on else "off")

        # --- persist processed state ---
        store.mark_processed(
            rec["id"], datetime.now(timezone.utc).isoformat(),
            cfg.model.name, cfg.model.version, params_json)
        store.commit()

        elapsed = perf_counter() - file_start
        rtf = dur / elapsed if elapsed > 0 else float("nan")
        total_events += len(events)
        total_audio += dur
        log.info("[%d/%d] %s done - %d events in %s  (%.1fx realtime)",
                 n, len(recs), rec["original_filename"], len(events),
                 _fmt_hms(elapsed), rtf)

    run_elapsed = perf_counter() - run_start
    log.info("  analyzed %d recordings (%s of audio) in %s  (%.1fx realtime), "
             "%d events total", len(recs), _fmt_hms(total_audio),
             _fmt_hms(run_elapsed),
             total_audio / run_elapsed if run_elapsed > 0 else float("nan"),
             total_events)
    # Record the segmentation that produced these events (for the guard).
    store.set_meta("segmentation_fingerprint", fingerprint)
    store.commit()
    return {"recordings": len(recs), "events": total_events}
