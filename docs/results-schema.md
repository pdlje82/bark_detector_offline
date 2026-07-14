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
| `schema_version` | int | Contract version. Frontend should check it (currently `2`). |
| `generated_at` | string (UTC) | When this file was exported. |
| `timezone` | string | IANA zone for all local times (e.g. `Europe/Berlin`). |
| `dogs` | array of string | The dog roster (real names) — options for the labeling dropdown. |
| `identification_metrics` | object \| null | How reliable the dog classifier is (see below). `null` until trained. |
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
| `by_dog` | object | `{ dog_name: count }` — events attributed to each dog that day (human or predicted; excludes unsure/multiple/not-a-dog). May be empty. |

## `events[]`

| Field | Type | Meaning |
|---|---|---|
| `id` | int | Row id (not stable across DB rebuilds — use `key` for labels). |
| `key` | string | **Stable** event id (`<sha12>_<offsetms>`). Use this as the label key. |
| `recording` | string | Source `original_filename`. |
| `abs_start_utc` / `abs_start_local` | string | When the bark occurred. |
| `abs_end_utc` | string | End of the event. |
| `duration_sec` | float | Event length. |
| `peak_conf` / `mean_conf` | float 0..1 | Detection **confidence** (not loudness). |
| `class` | string | Dominant dog class (`Bark`, `Howl`, `Yip`, …). |
| `night` | bool | Fell in the night window. |
| `intensity_relative` | float 0..1 \| null | Loudness relative to the loudest bark in scope (1.0 = loudest). |
| `intensity_dbfs` | float \| null | Absolute loudness in dBFS (comparable across files). |
| `dog_label` | string \| null | **Primary** resolved dog (first of `dog_labels`) — kept for back-compat. A roster name, `unsure`/`multiple`/`not_a_dog`, or null. |
| `dog_labels` | array of string | All attributed labels. Usually one; **two or more** when a human labelled multiple dogs in the clip. `[]` if unattributed. Counts credit each dog. |
| `dog_confidence` | float 0..1 \| null | Model confidence when predicted; null for human labels. |
| `dog_label_source` | string \| null | `human` (confirmed by a listener) or `predicted` (model suggestion) or null. **Render `predicted` as a suggestion, never as fact.** |
| `snippet_url` | string \| null | Path to the playable clip, relative to `results.json` (e.g. `snippets/<hash>/<name>.mp3`). |

## `identification_metrics` (dog-classifier reliability)

`null` until a classifier has been trained. When present:

| Field | Type | Meaning |
|---|---|---|
| `trained` | bool | Whether a model was trained. If `false`, see `reason` + `label_counts`. |
| `trained_at` | string (UTC) | When the model was trained. |
| `classifier` / `embedding` | string | The classifier + embedding backend used. |
| `dogs` | array | Dogs the model can distinguish (those above `min_labels_per_dog`). |
| `n_labeled` | int | Number of labeled examples used for training. |
| `label_counts` | object | `{ dog: count }` of all human labels (incl. dogs below the threshold). |
| `cv_available` | bool | Whether cross-validation was possible (needs ≥2 examples/dog). |
| `cv_folds` | int | Number of stratified folds used. |
| `accuracy` | float 0..1 | **Cross-validated** accuracy on held-out labels (the headline reliability figure). |
| `labels` | array | Row/column order of the confusion matrix. |
| `confusion_matrix` | 2-D int array | `matrix[i][j]` = true `labels[i]` predicted as `labels[j]`. Diagonal = correct. |
| `per_dog` | object | Per-dog `{ precision, recall, f1, support }` from cross-validation. |

These are **honest held-out estimates**, not resubstitution. Present accuracy and
the confusion matrix as the reliability of the *suggested* per-dog attribution;
human-confirmed labels are unaffected by model quality.

## Label API (frontend ↔ backend, live)

The primary labeling path is a small local server (`python -m barkdetect serve`)
that reads/writes human labels **directly in `barks.db`** — the single source of
truth. The frontend uses it in *labeling mode*; a hosted static copy without the
API is *read-only*.

| Method / route | Body | Effect |
|---|---|---|
| `GET /api/labels` | — | Returns `{ event_key: label }`; a multi-dog label comes back as an array. |
| `PUT /api/labels/<event_key>` | `{ "label": "Podenco" \| ["Podenco","Clooney"] }` | Create/update the label. |
| `DELETE /api/labels/<event_key>` | — | Remove the label. |

Label values are a dog name, a list of names, or one of `unsure`/`multiple`/
`not_a_dog`. Because writes hit the DB immediately, there is no export/import step
and deletions propagate.

## `labels.json` (legacy, optional import)

Still supported for a one-off bulk import if a file is present at
`identification.labels_path`, but no longer the primary path (the API is). The
pipeline merges it (additively), trains the classifier, and predicts per event.

```json
{
  "schema": 1,
  "exported_at": "2026-07-13T21:00:00Z",
  "labels": {
    "<event.key.a>": "Podenco",
    "<event.key.b>": ["Podenco", "Clooney"],
    "<event.key.c>": "not_a_dog"
  }
}
```
`labels` maps each event's stable `key` to **either a single label (string) or a
list of dog names** when more than one dog is audible. Each value is a roster name
(or list of them) ∪ {`unsure`, `multiple`, `not_a_dog`}. Human labels always
override predictions. Note: multi-dog clips are **excluded from classifier
training** (a mixture isn't a clean single-dog example) but still count toward
each named dog's totals.

## Serving

Place `results.json`, and the `snippets/` folder it references, together so
`snippet_url` resolves relative to the page. The frontend `fetch`es
`./results.json` (or wherever it's deployed) and plays `snippet_url` via `<audio>`.

## Notes for consumers

- `intensity_relative` in `per_file` scope makes each file's loudest bark = 1.0,
  so it ranks **within** a file; use `intensity_dbfs` for cross-file comparison.
- `intensity_*` may be `null` for events from older data without loudness.
- Confidence ≠ loudness — keep them as separate columns.
