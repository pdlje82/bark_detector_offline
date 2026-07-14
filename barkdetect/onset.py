"""Onset sub-segmentation: split a detected dog region into individual barks.

PANNs marks *regions* of barking but doesn't resolve rapid/overlapping barks
(its smoothed score stays high across a burst). Each bark is a sharp energy
onset in the RAW waveform, so we peak-pick the onset-strength envelope of the
region audio and cut at each onset. Only supplies split points — the caller
recomputes per-slice features from the existing frame arrays.

Separates *sequential* barks (incl. different dogs alternating); *simultaneous*
barks are one mixed sound and stay a single slice.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .audio import read_segment

log = logging.getLogger(__name__)


def _enforce_min_interval(times: list[float], min_interval: float) -> list[float]:
    """Keep onsets in order, dropping any within `min_interval` of the last kept.

    Pure and deterministic (no I/O) so it is unit-testable on plain lists.
    """
    kept: list[float] = []
    for t in sorted(times):
        if not kept or (t - kept[-1]) >= min_interval:
            kept.append(t)
    return kept


def find_onsets(audio_path: str | Path, start_sec: float, dur_sec: float, cfg) -> list[float]:
    """Return split offsets (seconds, relative to region start) for one region.

    Always includes 0.0 (region start). Onsets closer than
    `onset.min_interval_seconds` are merged; results are clipped to `dur_sec`.
    """
    import librosa
    sr = cfg.audio.sample_rate
    y = read_segment(audio_path, sr, start_sec, dur_sec)
    if y.size < sr // 20:                      # <50 ms — nothing to split
        return [0.0]
    env = librosa.onset.onset_strength(y=y, sr=sr)
    onsets = librosa.onset.onset_detect(
        onset_envelope=env, sr=sr, units="time",
        delta=float(cfg.onset.delta), backtrack=False)
    boundaries = _enforce_min_interval([0.0] + [float(t) for t in onsets],
                                       float(cfg.onset.min_interval_seconds))
    return [b for b in boundaries if b < dur_sec] or [0.0]


def save_debug_plot(audio_path, start_sec, dur_sec, boundaries, out_path, cfg) -> None:
    """Save waveform + onset-strength envelope with the kept onsets marked."""
    import librosa
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sr = cfg.audio.sample_rate
    y = read_segment(audio_path, sr, start_sec, dur_sec)
    if y.size == 0:
        return
    env = librosa.onset.onset_strength(y=y, sr=sr)
    t_wave = np.arange(y.size) / sr
    t_env = librosa.times_like(env, sr=sr)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 4), sharex=True)
    ax1.plot(t_wave, y, linewidth=0.5, color="#3b4a6b")
    ax1.set_ylabel("waveform")
    ax1.set_title(f"onsets: {len(boundaries)}  (region +{start_sec:.1f}s, {dur_sec:.2f}s)")
    ax2.plot(t_env, env, linewidth=0.8, color="#b7772f")
    ax2.set_ylabel("onset strength")
    ax2.set_xlabel("seconds (region-relative)")
    for b in boundaries:
        for ax in (ax1, ax2):
            ax.axvline(b, color="#2f8a5b", linewidth=0.9, alpha=0.8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=90)
    plt.close(fig)
