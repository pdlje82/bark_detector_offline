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


def embed_samples(samples: np.ndarray, cfg) -> np.ndarray:
    """Return the embedding vector for an already-decoded mono clip.

    Dispatches on cfg.identification.embedding. Use this when the audio is
    already in memory (e.g. sliced from a single per-region decode) so no extra
    ffmpeg decode is spawned. Only 'librosa' is implemented; 'panns'/'aves'
    raise until wired, keeping the interface stable.
    """
    backend = cfg.identification.embedding
    if backend == "librosa":
        return _librosa_features(samples, cfg.audio.sample_rate)
    raise NotImplementedError(
        f"embedding backend '{backend}' not implemented yet (use 'librosa')")


def embed_segment(audio_path: str | Path, start_sec: float, dur_sec: float,
                  cfg) -> np.ndarray:
    """Return the embedding vector for one bark event, decoding it from a file.

    Convenience wrapper that decodes exactly [start, start+dur] and embeds it.
    The analyze path uses `embed_samples` on an in-memory slice instead, to
    avoid a per-event ffmpeg decode.
    """
    samples = _read_segment(audio_path, cfg.audio.sample_rate, start_sec, dur_sec)
    return embed_samples(samples, cfg)
