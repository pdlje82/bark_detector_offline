"""Extract short, playable audio clips for each detected bark event."""

from __future__ import annotations

import math
import subprocess
from datetime import datetime
from pathlib import Path


def _fmt_dbfs(intensity_raw: float | None) -> str:
    """Format a linear amplitude (0..1) as an integer-dBFS token, floored at -120."""
    dbfs = round(20 * math.log10(intensity_raw)) if intensity_raw and intensity_raw > 0 else -120
    return f"{int(dbfs)}dBFS"


def snippet_relpath(cfg, sha256: str, event_local: datetime,
                    offset_start_sec: float, intensity_raw: float | None) -> str:
    """Relative path (under snippets_dir) for an event clip.

    The filename is built from cfg.snippets.name_template. Available tokens:
      {date}  event date, ddmmyy       {ms}    offset within recording, 9-digit ms
      {time}  event time, hhmmss        {dbfs}  loudness, e.g. "-29dBFS"
      {intensity} linear amplitude (dot->'p')   {hash}  short sha
    Clips stay under a per-recording "<hash>/" folder, and {ms} keeps names
    unique even when several barks fall in the same second.
    """
    ms = int(round(offset_start_sec * 1000))
    tokens = {
        "date": event_local.strftime("%d%m%y"),
        "time": event_local.strftime("%H%M%S"),
        "ms": f"{ms:09d}",
        "dbfs": _fmt_dbfs(intensity_raw),
        "intensity": f"{(intensity_raw or 0.0):.4f}".replace(".", "p"),
        "hash": sha256[:12],
    }
    stem = cfg.snippets.name_template.format(**tokens)
    return f"{sha256[:12]}/{stem}.{cfg.snippets.extension}"


def extract_snippet(source_mp3: str | Path, snippets_dir: Path, rel_path: str,
                    start_sec: float, duration_sec: float, cfg) -> str:
    """Cut [start-pad, end+pad] from the archived MP3 into a small clip.

    Returns rel_path. Uses input seeking (`-ss` before `-i`) so seeking into a
    24h file is fast; accuracy is well within the padding we add.

    The archived original is never modified. If snippets.normalize is set, the
    clip's loudness is normalized (ffmpeg loudnorm) so faint barks are audible;
    this treatment is recorded in results.json provenance.
    """
    sc = cfg.snippets
    ss = max(0.0, start_sec - sc.padding_seconds)
    dur = duration_sec + 2 * sc.padding_seconds
    out = snippets_dir / rel_path
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y",
           "-ss", f"{ss:.3f}", "-i", str(source_mp3), "-t", f"{dur:.3f}",
           "-ac", str(sc.channels)]
    if getattr(sc, "normalize", False):
        cmd += ["-af", f"loudnorm=I={sc.normalize_target_lufs}:TP=-1.5:LRA=11"]
    cmd += ["-c:a", sc.codec, "-q:a", str(sc.quality), str(out)]
    subprocess.run(cmd, check=True)
    return rel_path
