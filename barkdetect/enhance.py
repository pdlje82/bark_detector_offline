"""Produce a per-recording enhanced "working copy" from the raw archive.

The enhanced copy carries the audio processing that used to be baked into clips
(loudness normalization now; denoise/band-pass wired but off). Single-clip
playback (and, if `apply_to` includes them, the models) read this copy; window/
burst playback and loudness measurement always use the raw archive.

Run once per recording, offline — never in the interactive playback path.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from time import perf_counter

from .store import Store

log = logging.getLogger(__name__)


def _as_dict(x):
    """Accept a SimpleNamespace (from config) or a plain dict; return a dict."""
    if x is None:
        return {}
    return dict(vars(x)) if not isinstance(x, dict) else x


def filter_string(chain) -> str:
    """Translate the configured enhancement chain into an ffmpeg -af string."""
    parts = []
    for entry in chain or []:
        for name, params in _as_dict(entry).items():
            p = _as_dict(params)
            if name == "loudnorm":
                parts.append("loudnorm=" + ":".join(f"{k}={v}" for k, v in p.items()))
            elif name == "denoise":
                parts.append(str(p.get("filter", "afftdn")))
            elif name == "bandpass":
                if p.get("low"):
                    parts.append(f"highpass=f={p['low']}")
                if p.get("high"):
                    parts.append(f"lowpass=f={p['high']}")
            else:
                log.warning("  unknown enhancement filter '%s' — skipped", name)
    return ",".join(parts)


def enhanced_path(cfg, archived_path: str) -> Path:
    """Where the enhanced working copy for a given archived recording lives."""
    return cfg.resolve_path(cfg.enhancement.dir) / (Path(archived_path).stem + "." + cfg.enhancement.format)


def enhance(cfg, store: Store) -> dict:
    """Render an enhanced copy per recording (idempotent). Returns a summary."""
    if not cfg.enhancement.enabled:
        log.info("  enhancement disabled")
        return {"built": 0, "skipped": 0}

    af = filter_string(cfg.enhancement.chain)
    out_dir = cfg.resolve_path(cfg.enhancement.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("  enhancement chain: %s", af or "(none)")

    built = skipped = 0
    for rec in store.all_recordings():
        src = Path(rec["archived_path"])
        dst = enhanced_path(cfg, rec["archived_path"])
        if dst.exists() and src.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            skipped += 1
            continue
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)]
        if af:
            cmd += ["-af", af]
        cmd += ["-ac", "1", str(dst)]      # mono working copy
        t = perf_counter()
        subprocess.run(cmd, check=True)
        built += 1
        log.info("  enhanced %s -> %s  (%.1fs)", src.name, dst.name, perf_counter() - t)

    log.info("  enhancement: %d built, %d up-to-date", built, skipped)
    return {"built": built, "skipped": skipped}
