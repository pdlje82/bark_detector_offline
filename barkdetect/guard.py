"""Segmentation guard: protect human labels from silent invalidation.

Labels are keyed by ``event_key = <sha12>_<offset_ms>``. Changing any parameter
that moves event boundaries (detection thresholds, merge/min gaps, normalization,
audio windowing, dog classes) shifts offsets and orphans existing labels. This
module fingerprints those parameters and forces an explicit confirmation before
clearing labels, so tuning-phase experiments are deliberate, not accidental.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys

from .store import Store

log = logging.getLogger(__name__)

ASSUME_YES_ENV = "BARKDETECT_ASSUME_YES"


def segmentation_fingerprint(cfg) -> str:
    """Stable hash of the parameters that determine event identity.

    Snippet/intensity/identification params are intentionally excluded — they do
    not change event boundaries, so tweaking them never invalidates labels.
    """
    payload = {
        "detection": {
            "threshold": cfg.detection.threshold,
            "min_event_seconds": cfg.detection.min_event_seconds,
            "merge_gap_seconds": cfg.detection.merge_gap_seconds,
            "dog_classes": list(cfg.detection.dog_classes),
        },
        "normalization": dict(vars(cfg.normalization)),
        "audio": {
            "sample_rate": cfg.audio.sample_rate,
            "window_seconds": cfg.audio.window_seconds,
            "min_window_seconds": cfg.audio.min_window_seconds,
        },
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def enforce_guard(cfg, store: Store, fingerprint: str) -> None:
    """Abort or clear labels if segmentation changed since labels were made.

    Only acts when a fingerprint is already stored, it differs, AND labels exist.
    Confirmation: BARKDETECT_ASSUME_YES=1 auto-confirms; an interactive TTY is
    prompted; no TTY aborts (never silently wipes).
    """
    stored = store.get_meta("segmentation_fingerprint")
    n = store.count_labels()
    if not stored or stored == fingerprint or n == 0:
        return

    log.warning("  segmentation parameters changed since %d label(s) were created.",
                n)
    log.warning("  re-segmenting will shift event boundaries and ORPHAN those labels.")

    if os.environ.get(ASSUME_YES_ENV) == "1":
        confirmed = True
        log.warning("  %s=1 set — auto-confirming label clear.", ASSUME_YES_ENV)
    elif sys.stdin is not None and sys.stdin.isatty():
        resp = input(f"Type 'yes' to CLEAR {n} label(s) and continue: ")
        confirmed = resp.strip().lower() == "yes"
    else:
        raise SystemExit(
            f"Segmentation changed and {n} labels exist, but no interactive "
            f"terminal to confirm. Re-run in a terminal, or set {ASSUME_YES_ENV}=1 "
            f"to clear labels non-interactively. Nothing was changed.")

    if not confirmed:
        raise SystemExit("Aborted: segmentation change not confirmed. "
                         "Labels and data left unchanged.")
    cleared = store.clear_labels()
    log.warning("  cleared %d label(s) due to segmentation change.", cleared)
