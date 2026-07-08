"""Run detection over unprocessed recordings and persist events + snippets.

Also records the exact parameters (model, normalization, detection) that
produced each recording's events, for chain-of-custody / reproducibility.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import detect
from .snippets import extract_snippet, snippet_relpath
from .store import Store


def _abs_iso(start_utc_iso: str, offset_sec: float) -> str:
    base = datetime.fromisoformat(start_utc_iso)
    return (base + timedelta(seconds=offset_sec)).isoformat()


def analyze(cfg, store: Store) -> dict:
    recs = store.unprocessed_recordings()
    if not recs:
        print("  no unprocessed recordings")
        return {"recordings": 0, "events": 0}

    print(f"  loading model ({cfg.model.name}) on {cfg.model.device} ...")
    model, labels = detect.load_model(
        device=cfg.model.device, checkpoint_path=cfg.model.checkpoint_path)
    dog_idx = detect.resolve_dog_indices(labels, list(cfg.detection.dog_classes))
    dog_names = list(cfg.detection.dog_classes)
    snippets_dir = cfg.path("snippets_dir")
    params_json = json.dumps(cfg.params_snapshot(), ensure_ascii=False)

    total_events = 0
    for rec in recs:
        print(f"  analyzing {rec['original_filename']} ...", flush=True)
        times, scores, best = detect.score_recording(
            rec["archived_path"], cfg, model, dog_idx)
        events = detect.extract_events(times, scores, best, dog_names, cfg)

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
                "snippet_path": rel,
            })
        store.mark_processed(
            rec["id"], datetime.now(timezone.utc).isoformat(),
            cfg.model.name, cfg.model.version, params_json)
        store.commit()
        total_events += len(events)
        print(f"    {len(events)} bark events")

    return {"recordings": len(recs), "events": total_events}
