"""Dog-bark detection using PANNs Sound Event Detection (frame-level)."""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
from tqdm import tqdm

from .audio import normalize_window, stream_windows

log = logging.getLogger(__name__)


def _fmt_hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def load_model(device: str = "cpu", checkpoint_path: str | None = None):
    """Instantiate the PANNs SED model and return (model, labels)."""
    from panns_inference import SoundEventDetection
    try:
        from panns_inference import labels
    except ImportError:  # older/newer layouts
        from panns_inference.config import labels
    try:
        model = SoundEventDetection(checkpoint_path=checkpoint_path, device=device)
    except (RuntimeError, EOFError) as e:
        # A truncated/corrupt .pth (e.g. an interrupted download) surfaces here
        # as an opaque torch error. Give an actionable message instead.
        raise RuntimeError(
            "Failed to load the PANNs checkpoint — it looks incomplete or "
            "corrupt (common with an interrupted download). Delete the file at "
            "~/panns_data/Cnn14_DecisionLevelMax.pth (or model.checkpoint_path) "
            f"and re-download it fully, then retry. Original error: {e}"
        ) from e
    return model, list(labels)


def resolve_dog_indices(labels: list[str], dog_classes: list[str]) -> list[int]:
    """Map configured dog class names to PANNs label indices."""
    idx = []
    missing = []
    for name in dog_classes:
        if name in labels:
            idx.append(labels.index(name))
        else:
            missing.append(name)
    if missing:
        raise ValueError(
            f"Dog classes not found in PANNs labels: {missing}. "
            f"Check spelling in config.yml against the AudioSet ontology."
        )
    return idx


def score_recording(path: str | Path, cfg, model, dog_idx: list[int],
                    duration_sec: float | None = None):
    """Run the model over the whole file, returning per-frame arrays.

    Returns (times, scores, best_dog_col):
      times          absolute offset (s) of each frame within the recording
      scores         max dog-class probability per frame
      best_dog_col   index into dog_idx of the strongest dog class per frame

    A live tqdm progress bar (total windows known from duration_sec) is shown
    unless disabled via cfg.logging.progress_bar.
    """
    sr = cfg.audio.sample_rate
    window_sec = cfg.audio.window_seconds
    min_samples = int(cfg.audio.min_window_seconds * sr)

    total_windows = (math.ceil(duration_sec / window_sec)
                     if duration_sec else None)
    windows = stream_windows(path, sr, window_sec)
    if cfg.logging.progress_bar:
        windows = tqdm(windows, total=total_windows, unit="win",
                       desc=Path(path).name, leave=False)

    all_times, all_scores, all_best = [], [], []
    for win_start, arr in windows:
        if arr.size < min_samples:
            continue
        if duration_sec:
            log.debug("  at %s / %s", _fmt_hms(win_start), _fmt_hms(duration_sec))
        arr = normalize_window(arr, cfg.normalization)
        framewise = model.inference(arr[None, :])          # (1, F, C)
        framewise = np.asarray(framewise)[0]               # (F, C)
        dog = framewise[:, dog_idx]                         # (F, ndog)
        frame_score = dog.max(axis=1)
        frame_best = dog.argmax(axis=1)
        F = framewise.shape[0]
        dt = (arr.size / sr) / F
        times = win_start + np.arange(F) * dt
        all_times.append(times)
        all_scores.append(frame_score)
        all_best.append(frame_best)

    if not all_times:
        return np.array([]), np.array([]), np.array([], dtype=int)
    return (np.concatenate(all_times),
            np.concatenate(all_scores),
            np.concatenate(all_best))


def extract_events(times, scores, best, dog_class_names, cfg) -> list[dict]:
    """Turn a per-frame score timeline into discrete bark events."""
    if times.size == 0:
        return []

    threshold = cfg.detection.threshold
    merge_gap = cfg.detection.merge_gap_seconds
    min_dur = cfg.detection.min_event_seconds

    above = scores >= threshold

    # Contiguous runs of hot frames.
    runs = []
    start = None
    for i, hot in enumerate(above):
        if hot and start is None:
            start = i
        elif not hot and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(above) - 1))

    # Merge runs separated by a gap smaller than merge_gap.
    merged = []
    for r in runs:
        if merged and times[r[0]] - times[merged[-1][1]] <= merge_gap:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(r)

    events = []
    for a, b in merged:
        t0, t1 = float(times[a]), float(times[b])
        # end of the last frame ~ its start plus the local frame step
        if b + 1 < len(times):
            t1 = float(times[b + 1])
        dur = t1 - t0
        if dur < min_dur:
            continue
        seg = scores[a:b + 1]
        peak_i = a + int(np.argmax(seg))
        top_class = dog_class_names[int(best[peak_i])]
        events.append({
            "offset_start_sec": round(t0, 3),
            "offset_end_sec": round(t1, 3),
            "duration_sec": round(dur, 3),
            "peak_conf": round(float(seg.max()), 4),
            "mean_conf": round(float(seg.mean()), 4),
            "top_class": top_class,
        })
    return events
