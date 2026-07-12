"""Compute recording coverage intervals and gaps on the absolute timeline.

The Zoom H6 splits long recordings into consecutive ~2GB files; those are
contiguous and must NOT read as gaps. A true gap is a stretch of wall-clock
time with no recording at all (recorder off / between download sessions).
"""

from __future__ import annotations

from datetime import datetime, timedelta


def _parse(iso: str) -> datetime:
    """Parse an ISO-8601 string into a datetime."""
    return datetime.fromisoformat(iso)


def compute_coverage(recordings, merge_gap_seconds: float):
    """Return (coverage_intervals, gaps) as lists of dicts with UTC iso times."""
    spans = []
    for r in recordings:
        start = _parse(r["start_utc"])
        end = start + timedelta(seconds=r["duration_sec"])
        spans.append((start, end))
    spans.sort()

    if not spans:
        return [], []

    merged = [list(spans[0])]
    for start, end in spans[1:]:
        gap = (start - merged[-1][1]).total_seconds()
        if gap <= merge_gap_seconds:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])

    coverage = [
        {"start": s.isoformat(), "end": e.isoformat(),
         "duration_sec": round((e - s).total_seconds(), 1)}
        for s, e in merged
    ]
    gaps = []
    for (s1, e1), (s2, e2) in zip(merged, merged[1:]):
        gaps.append({
            "start": e1.isoformat(),
            "end": s2.isoformat(),
            "duration_sec": round((s2 - e1).total_seconds(), 1),
        })
    return coverage, gaps
