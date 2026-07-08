"""Unit tests for the pure logic (no audio model / ffmpeg required).

Run with:  python -m pytest   (or: python tests/test_logic.py)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from barkdetect.audio import normalize_window
from barkdetect.coverage import compute_coverage
from barkdetect.detect import extract_events


def _det_cfg(threshold=0.5, merge_gap=0.4, min_event=0.15):
    return SimpleNamespace(detection=SimpleNamespace(
        threshold=threshold, merge_gap_seconds=merge_gap,
        min_event_seconds=min_event))


# --- normalization ---------------------------------------------------------

def test_normalize_boosts_quiet_audio():
    cfg = SimpleNamespace(enabled=True, target_peak=0.9, max_gain=20.0, noise_floor=0.005)
    quiet = np.array([0.05, -0.05, 0.025], dtype=np.float32)
    out = normalize_window(quiet, cfg)
    assert abs(np.max(np.abs(out)) - 0.9) < 1e-5


def test_normalize_silence_guard():
    cfg = SimpleNamespace(enabled=True, target_peak=0.9, max_gain=20.0, noise_floor=0.005)
    silence = np.array([0.001, -0.002, 0.0], dtype=np.float32)
    out = normalize_window(silence, cfg)
    np.testing.assert_array_equal(out, silence)  # untouched


def test_normalize_gain_cap():
    cfg = SimpleNamespace(enabled=True, target_peak=0.9, max_gain=2.0, noise_floor=0.005)
    arr = np.array([0.1, -0.1], dtype=np.float32)  # would need 9x, capped at 2x
    out = normalize_window(arr, cfg)
    assert abs(np.max(np.abs(out)) - 0.2) < 1e-5


# --- event extraction ------------------------------------------------------

def test_extract_single_event():
    # 10 fps timeline, 2s of "hot" in the middle
    times = np.arange(0, 5, 0.1)
    scores = np.zeros_like(times)
    scores[20:40] = 0.8              # 2.0s .. 4.0s
    best = np.zeros_like(times, dtype=int)
    events = extract_events(times, scores, best, ["Dog", "Bark"], _det_cfg())
    assert len(events) == 1
    assert events[0]["top_class"] == "Dog"
    assert 1.9 < events[0]["offset_start_sec"] < 2.1
    assert events[0]["duration_sec"] > 1.5


def test_merge_close_events_and_drop_short():
    times = np.arange(0, 5, 0.1)
    scores = np.zeros_like(times)
    scores[10:13] = 0.9              # 0.3s span
    scores[15:18] = 0.9             # gap of 0.2s -> merged
    scores[45:46] = 0.9             # 0.1s lone spike -> dropped (min 0.15s)
    best = np.zeros_like(times, dtype=int)
    events = extract_events(times, scores, best, ["Dog"], _det_cfg())
    assert len(events) == 1         # two merged, short one dropped


# --- coverage & gaps -------------------------------------------------------

def test_coverage_gap_and_contiguous():
    recs = [
        # two back-to-back files (H6 split) -> one covered span, no gap
        {"start_utc": "2026-07-05T18:00:00+00:00", "duration_sec": 3600},
        {"start_utc": "2026-07-05T19:00:00+00:00", "duration_sec": 3600},
        # later file after a gap
        {"start_utc": "2026-07-06T08:00:00+00:00", "duration_sec": 1800},
    ]
    coverage, gaps = compute_coverage(recs, merge_gap_seconds=5.0)
    assert len(coverage) == 2       # [18:00-20:00], [08:00-08:30]
    assert len(gaps) == 1
    assert abs(gaps[0]["duration_sec"] - 12 * 3600) < 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
