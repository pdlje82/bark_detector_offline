"""Extract short, playable audio clips for each detected bark event."""

from __future__ import annotations

import subprocess
from pathlib import Path


def snippet_relpath(sha256: str, offset_start_sec: float, extension: str) -> str:
    """Deterministic relative path (under snippets_dir) for an event clip."""
    ms = int(round(offset_start_sec * 1000))
    return f"{sha256[:12]}/evt_{ms:09d}.{extension}"


def extract_snippet(source_mp3: str | Path, snippets_dir: Path, rel_path: str,
                    start_sec: float, duration_sec: float, cfg) -> str:
    """Cut [start-pad, end+pad] from the archived MP3 into a small clip.

    Returns rel_path. Uses input seeking (`-ss` before `-i`) so seeking into a
    24h file is fast; accuracy is well within the padding we add.
    """
    sc = cfg.snippets
    ss = max(0.0, start_sec - sc.padding_seconds)
    dur = duration_sec + 2 * sc.padding_seconds
    out = snippets_dir / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-ss", f"{ss:.3f}", "-i", str(source_mp3), "-t", f"{dur:.3f}",
           "-ac", str(sc.channels), "-c:a", sc.codec, "-q:a", str(sc.quality),
           str(out)]
    subprocess.run(cmd, check=True)
    return rel_path
