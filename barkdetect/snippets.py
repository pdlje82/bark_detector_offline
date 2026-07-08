"""Extract short, playable MP3 clips for each detected bark event."""

from __future__ import annotations

import subprocess
from pathlib import Path


def snippet_relpath(sha256: str, offset_start_sec: float) -> str:
    """Deterministic relative path (under snippets_dir) for an event clip."""
    ms = int(round(offset_start_sec * 1000))
    return f"{sha256[:12]}/evt_{ms:09d}.mp3"


def extract_snippet(source_mp3: str | Path, snippets_dir: Path, rel_path: str,
                    start_sec: float, duration_sec: float, cfg) -> str:
    """Cut [start-pad, end+pad] from the archived MP3 into a small clip.

    Returns rel_path. Uses input seeking (`-ss` before `-i`) so seeking into a
    24h file is fast; accuracy is well within the padding we add.
    """
    pad = cfg.snippets.padding_seconds
    ss = max(0.0, start_sec - pad)
    dur = duration_sec + 2 * pad
    out = snippets_dir / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-ss", f"{ss:.3f}", "-i", str(source_mp3), "-t", f"{dur:.3f}",
           "-ac", "1", "-c:a", "libmp3lame", "-q:a", str(cfg.snippets.quality),
           str(out)]
    subprocess.run(cmd, check=True)
    return rel_path
