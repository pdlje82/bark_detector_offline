"""Per-bark embeddings ("fingerprints") for individual-dog identification.

The embedding is a fixed-length vector computed from the RAW (un-normalized,
un-padded) audio of a single bark event. Backends are pluggable via
`identification.embedding`; v0 is a classic librosa acoustic feature set, which
needs no extra model download. The interface is identical across backends so the
rest of the pipeline never changes when the backend is swapped.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .audio import read_segment as _read_segment


def _librosa_features(samples: np.ndarray, sr: int) -> np.ndarray:
    """Fixed-length acoustic feature vector (MFCC + spectral + ZCR stats).

    Timbre-oriented and loudness-invariant, so it captures per-dog voice
    character rather than volume. Returns a deterministic-length 1-D vector.
    """
    import librosa
    if samples.size < sr // 20:  # pad very short clips to ~50 ms
        samples = np.pad(samples, (0, max(0, sr // 20 - samples.size)))
    mfcc = librosa.feature.mfcc(y=samples, sr=sr, n_mfcc=20)
    parts = [
        mfcc.mean(axis=1), mfcc.std(axis=1),
        librosa.feature.delta(mfcc).mean(axis=1),
        [librosa.feature.spectral_centroid(y=samples, sr=sr).mean()],
        [librosa.feature.spectral_bandwidth(y=samples, sr=sr).mean()],
        [librosa.feature.spectral_rolloff(y=samples, sr=sr).mean()],
        [librosa.feature.zero_crossing_rate(samples).mean()],
    ]
    vec = np.concatenate([np.asarray(p, dtype=np.float32).ravel() for p in parts])
    return np.nan_to_num(vec).astype(np.float32)


def embed_segment(audio_path: str | Path, start_sec: float, dur_sec: float,
                  cfg) -> np.ndarray:
    """Return the embedding vector for one bark event.

    Dispatches on cfg.identification.embedding. Only 'librosa' is implemented;
    'panns'/'aves' raise until wired, keeping the interface stable.
    """
    backend = cfg.identification.embedding
    sr = cfg.audio.sample_rate
    if backend == "librosa":
        samples = _read_segment(audio_path, sr, start_sec, dur_sec)
        return _librosa_features(samples, sr)
    raise NotImplementedError(
        f"embedding backend '{backend}' not implemented yet (use 'librosa')")
