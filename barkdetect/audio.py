"""Audio decoding, streaming and normalization via ffmpeg.

A 24h recording is far too large to hold in memory, so we pipe raw PCM out of
ffmpeg in a single pass and yield fixed-size windows. Each window is peak-
normalized before detection so that quiet recordings are boosted consistently.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterator

import numpy as np


def ffprobe_duration(path: str | Path) -> float:
    """Return media duration in seconds.

    Raises RuntimeError (not an opaque JSON/subprocess error) if ffprobe fails or
    the file has no readable duration, so callers can skip the file cleanly.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed ({out.returncode}): {out.stderr.strip()}")
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise RuntimeError(f"no readable duration ({e}); stdout={out.stdout!r}") from e


def _read_exact(stream, n: int) -> bytes:
    """Read up to n bytes, returning fewer only at EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def stream_windows(path: str | Path, sr: int, window_sec: float
                   ) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (start_offset_seconds, samples) windows of mono float32 audio.

    Decoding happens in a single forward pass, so memory stays flat regardless
    of file length. The final window may be shorter than window_sec.
    """
    win_samples = int(window_sec * sr)
    chunk_bytes = win_samples * 4  # float32
    cmd = ["ffmpeg", "-v", "error", "-i", str(path),
           "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(sr), "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    offset_samples = 0
    try:
        while True:
            buf = _read_exact(proc.stdout, chunk_bytes)
            if not buf:
                break
            arr = np.frombuffer(buf, dtype=np.float32).copy()
            yield offset_samples / sr, arr
            offset_samples += len(arr)
            if len(buf) < chunk_bytes:
                break
    finally:
        if proc.stdout:
            proc.stdout.close()
        proc.wait()


def read_segment(path: str | Path, sr: int, start_sec: float, dur_sec: float) -> np.ndarray:
    """Decode exactly [start, start+dur] of a file to mono float32 via ffmpeg.

    Input seeking (`-ss` before `-i`) makes grabbing a segment from a long
    recording fast (no decode-from-start). Shared by embeddings and onset.
    """
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start_sec):.3f}",
           "-i", str(path), "-t", f"{max(0.05, dur_sec):.3f}",
           "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(sr), "-"]
    out = subprocess.run(cmd, capture_output=True)
    return np.frombuffer(out.stdout, dtype=np.float32).copy()


def normalize_window(arr: np.ndarray, cfg) -> np.ndarray:
    """Peak-normalize a window so quiet audio is boosted before detection.

    A silence guard prevents amplifying near-silent windows into noise, and a
    max-gain cap avoids extreme amplification of faint background hum.
    """
    if not cfg.enabled or arr.size == 0:
        return arr
    peak = float(np.max(np.abs(arr)))
    if peak < cfg.noise_floor:
        return arr  # effectively silent — leave as-is
    gain = min(cfg.target_peak / peak, cfg.max_gain)
    return arr * gain
