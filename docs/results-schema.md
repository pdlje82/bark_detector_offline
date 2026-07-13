# `results.json` — data contract

This is the **only** interface between the backend and the frontend. The frontend
reads `results.json` (and the audio files it references) as static assets; it
never touches the database or Python. A companion example is
[`sample_results.json`](./sample_results.json) — build and test against it.

All times are ISO-8601. `*_utc` fields are UTC; `*_local` and the `start`/`end`
of coverage/gaps are in the file's `timezone`. Durations are seconds (float).

## Top level

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | Contract version. Frontend should check it (currently `1`). |
| `generated_at` | string (UTC) | When this file was exported. |
| `timezone` | string | IANA zone for all local times (e.g. `Europe/Berlin`). |
| `parameters` | object | Settings that produced this export (see below) — for provenance. |
| `recording_count` | int | Number of source recordings. |
| `event_count` | int | Number of bark events. |
| `recordings` | array | One entry per source recording (see below). |
| `coverage` | array | Continuous spans that were actually recorded (see below). |
| `gaps` | array | Spans with **no** recording between coverage spans. |
| `daily_summary` | array | Per-day aggregates. |
| `events` | array | The detected bark events (see below). |

## `parameters` (provenance)

The exact settings used, so any result is reproducible and defensible. Keys:
`model` (`name`, `version`, `device`), `normalization` (`enabled`, `target_peak`,
`max_gain`, `noise_floor`), `detection` (`threshold`, `min_event_seconds`,
`merge_gap_seconds`, `dog_classes[]`), `audio` (`sample_rate`, `window_seconds`),
`intensity` (`metric` = `rms`|`peak`, `scope` = `per_file`|`global`),
`snippets` (`normalized` bool, `target_lufs`). Treat as display-only key/values.

## `recordings[]`

| Field | Type | Meaning |
|---|---|---|
| `original_filename` | string | Original name on the SD card (e.g. `ZOOM0007.MP3`). |
| `sha256` | string | Integrity hash of the source file. |
| `start_utc` / `start_local` | string | Recording start. |
| `duration_sec` | float | Length. |
| `timestamp_source` | string | How start was derived (e.g. `sdcard_ctime`). |
| `processed_at` | string \| null | When analyzed (null if not yet). |
| `model_name` / `model_version` | string \| null | Model used. |
| `parameters` | object \| null | Parameters that produced *this* recording's events. |

## `coverage[]` and `gaps[]`

Both are `{ "start": <local>, "end": <local>, "duration_sec": float }`.
`coverage` = time that was recorded (contiguous files merged). `gaps` = time with
no recording. **Render gaps explicitly** — silence in a gap means "not recorded",
not "no barking".

## `daily_summary[]`

| Field | Type | Meaning |
|---|---|---|
| `date` | string (YYYY-MM-DD, local) | The day. |
| `count` | int | Bark events that day. |
| `total_bark_seconds` | float | Summed event duration. |
| `night_count` | int | Events in the night window (config `night_start_hour`..`night_end_hour`). |

## `events[]`

| Field | Type | Meaning |
|---|---|---|
| `id` | int | Stable id. |
| `recording` | string | Source `original_filename`. |
| `abs_start_utc` / `abs_start_local` | string | When the bark occurred. |
| `abs_end_utc` | string | End of the event. |
| `duration_sec` | float | Event length. |
| `peak_conf` / `mean_conf` | float 0..1 | Detection **confidence** (not loudness). |
| `class` | string | Dominant dog class (`Bark`, `Howl`, `Yip`, …). |
| `night` | bool | Fell in the night window. |
| `intensity_relative` | float 0..1 \| null | Loudness relative to the loudest bark in scope (1.0 = loudest). |
| `intensity_dbfs` | float \| null | Absolute loudness in dBFS (comparable across files). |
| `snippet_url` | string \| null | Path to the playable clip, relative to `results.json` (e.g. `snippets/<hash>/<name>.mp3`). |

## Serving

Place `results.json`, and the `snippets/` folder it references, together so
`snippet_url` resolves relative to the page. The frontend `fetch`es
`./results.json` (or wherever it's deployed) and plays `snippet_url` via `<audio>`.

## Notes for consumers

- `intensity_relative` in `per_file` scope makes each file's loudest bark = 1.0,
  so it ranks **within** a file; use `intensity_dbfs` for cross-file comparison.
- `intensity_*` may be `null` for events from older data without loudness.
- Confidence ≠ loudness — keep them as separate columns.
