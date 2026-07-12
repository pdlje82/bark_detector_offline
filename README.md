# Bark Detector Offline

Analyze Zoom H6 24/7 MP3 recordings for **dog barking**, to document when and how
much a neighbour's dogs bark. Produces a JSON results file plus playable audio
snippets for a frontend to present as evidence.

Designed for **integrity**: original files are hashed (SHA-256) and copied into an
immutable archive, recording start times come from the SD card's own filesystem
timestamps, every detection has a listenable clip, and the whole pipeline is
reproducible and re-runnable (idempotent).

## What it does

1. **Ingest** — reads MP3s directly from the SD card, hashes them, derives the
   recording start time from the card's creation timestamp, and copies each file
   into `data/archive/`.
2. **Analyze** — streams each recording through **PANNs** (a pretrained AudioSet
   sound-event model). Audio is **peak-normalized per window first** so quiet
   recordings are boosted, then dog-related classes (Dog, Bark, Bow-wow, Yip,
   Howl, Growling, Whimper) are thresholded and merged into discrete bark events.
   Tuned for **high recall** — faint/ambiguous barks are kept and filtered later
   by listening to the snippets.
3. **Snippets** — a short MP3 clip (event ± padding) is cut for every event.
4. **Export** — everything is written to `data/export/results.json`: bark events
   (with absolute local timestamps and snippet URLs), **recording coverage and
   gaps**, and per-day summaries (incl. night counts).

## How inference works

The analyze step is the heavy part. It is designed to process arbitrarily long
recordings (24 h+) on a CPU with flat memory and predictable progress.

**1. Single-pass streaming decode.** A recording is never loaded whole. `ffmpeg`
decodes the MP3 in one forward pass to mono 32 kHz float32 PCM (PANNs' required
format) and pipes it out; `audio.stream_windows` reads that pipe in fixed
`audio.window_seconds` chunks (default 60 s). Memory stays constant regardless of
file length — a 24 h file uses the same RAM as a 1 min file.

**2. Per-window normalization.** Each window is peak-normalized before detection
(`audio.normalize_window`): quiet audio is boosted toward `target_peak`, capped by
`max_gain`, and a `noise_floor` guard leaves near-silent windows untouched so
background hiss is not amplified into false positives.

**3. Model inference (the bottleneck).** Each window is run through the PANNs
`Cnn14_DecisionLevelMax` Sound Event Detection model, which returns a frame-level
probability (~100 frames/s) for all 527 AudioSet classes. This forward pass is
what makes analysis slow on CPU; everything else is negligible. For each frame we
keep only the **max probability across the configured `dog_classes`** and which
dog class won.

**4. Timeline assembly & event extraction.** Per-window frame scores are
concatenated into one continuous timeline for the whole file. `detect.extract_events`
then: thresholds frames at `detection.threshold` (low = high recall), merges hot
runs closer than `merge_gap_seconds`, and drops anything shorter than
`min_event_seconds`. Each surviving run becomes one bark **event** with peak/mean
confidence, its dominant class, and start/end offsets. Because the timeline is
continuous, a bark straddling a window boundary is still detected as one event.
In parallel, a raw-loudness envelope (measured from the **un-normalized** audio,
so it reflects true relative volume) is captured on the same timeline, and each
event gets the peak loudness within its span — later turned into
`intensity_relative` (0–1) and `intensity_dbfs` at export (see `intensity`).

**5. Absolute timing, snippets, provenance.** Event offsets are added to the
recording's start time to get absolute UTC/local timestamps. A padded MP3 clip is
cut per event for later listening — by default its loudness is normalized
(`snippets.normalize`) so faint barks in quiet recordings are audible, while the
archived original is left untouched. The exact parameter set used (including
whether clips were loudness-boosted) is stored with the recording (see
`parameters` in `results.json`) so results are reproducible.

Latency scales with audio length, not file count. Tuning knobs: raise
`window_seconds` to reduce per-call overhead, or (not yet wired) increase
`torch` CPU threads.

### Logging & progress

Analysis logs each stage and its timing, plus a **realtime factor** (audio time
processed per wall-clock second) so remaining time is predictable — e.g.
`ZOOM0007.MP3 done — 128 events in 12m23s (34.8x realtime)` means a 7 h file
takes ~12 min. A live per-file progress bar (`tqdm`) shows percentage and ETA.

Configure via the `logging` block in `config.yml`:

```yaml
logging:
  level: INFO            # DEBUG adds a per-window "at HH:MM:SS / HH:MM:SS" position line
  progress_bar: true     # live tqdm bar; set false for headless/cron/redirected runs
  log_file: data/processing.log  # timestamped audit trail (appended); null = console only
```

The `log_file` doubles as a processing audit trail for evidence: a timestamped
record of when each file was analyzed and with what result.

## Setup (conda / mamba)

```bash
mamba env create -f environment.yml
mamba activate bark-detector-offline
# quick check
python -c "import torch, panns_inference, librosa, soundfile; print('ok')"
```

`ffmpeg` is installed into the env by conda — no separate install needed. The
PANNs model checkpoint (~300 MB) downloads automatically on first `analyze`.

Edit `config.yml` before first use — at minimum set `run.source` (the SD card
path), `timezone` to match the recorder's clock, and confirm the detection
`threshold`.

## Usage

The pipeline takes **no command-line arguments** — everything is configured in
`config.yml`. Set `run.source` and `run.steps`, then run:

```bash
python -m barkdetect
```

`run.steps` controls what happens, in order. For the normal every-few-days
operation, leave it as:

```yaml
run:
  source: "E:/DCIM"
  steps: [ingest, analyze, export]
```

To only re-export: `steps: [export]`. To re-analyze without re-copying:
`steps: [analyze, export]`. To use an alternate config file:

```bash
BARKDETECT_CONFIG=other.yml python -m barkdetect
```

Re-running is safe: files already ingested (same SHA-256) are skipped, and only
unprocessed recordings are analyzed.

## Output layout

```
data/
  archive/            immutable copies of the original MP3s
  snippets/<hash>/    per-event .mp3 clips (referenced by results.json)
  barks.db            SQLite source of truth
  export/results.json frontend input
```

`results.json` contains: `parameters` (the model/normalization/detection
settings used — recorded for reproducibility), `recordings`, `coverage`, `gaps`,
`daily_summary`, and `events` (each with `abs_start_local`, `class`, `peak_conf`,
`night`, `intensity_relative` (0–1, loudest bark in scope = 1), `intensity_dbfs`
(absolute loudness), `snippet_url`). Each recording also carries the exact
`parameters` that produced its events. Serve `data/export/` and `data/snippets/` as static files
to the frontend.

## Configuration reference (`config.yml`)

Everything is set in `config.yml` — the pipeline takes no CLI arguments. Below is
every parameter, its default, and what it does. Paths are relative to the project
root unless absolute. 🔧 marks the settings you are most likely to tune;
⚠️ marks settings you should normally leave alone.

### `run` — what runs

| Key | Default | Meaning |
|-----|---------|---------|
| `source` 🔧 | `E:/DCIM` | Folder/SD-card path to ingest from. Used only by the `ingest` step. Point it at the card itself (timestamps live there). |
| `steps` 🔧 | `[ingest, analyze, export]` | Which stages run, in order. E.g. `[export]` to only rebuild JSON, `[analyze, export]` to re-run detection without re-copying. |

### `paths` — where data lives

The four data paths below are resolved against `root`; absolute values are used
as-is. Set `root` to your project/data directory to keep data **out of the git
repo**.

| Key | Default | Meaning |
|-----|---------|---------|
| `root` 🔧 | `D:/Projects/dog_bark` | Base dir the paths below resolve against. `null`/empty = the repo dir (where `config.yml` lives). |
| `archive_dir` | `data/archive` | Immutable copies of the original MP3s. Auto-excluded from ingest scans even if it lives inside `run.source`. |
| `snippets_dir` | `data/snippets` | Per-event audio clips (served to the frontend). |
| `db_path` | `data/barks.db` | SQLite source of truth. |
| `export_dir` | `data/export` | Where `results.json` is written. |

### `timezone`

| Key | Default | Meaning |
|-----|---------|---------|
| `timezone` 🔧 | `Europe/Berlin` | IANA zone of the recorder **and** this PC. Must match the H6's clock — used to turn SD-card FAT timestamps into correct local/UTC times. |

### `model` — the detector

| Key | Default | Meaning |
|-----|---------|---------|
| `device` 🔧 | `cpu` | `cpu` or `cuda` (needs a CUDA-enabled PyTorch + GPU). |
| `name` | `PANNs Cnn14_DecisionLevelMax` | Label stored with results; not a switch to another model. |
| `version` | `audioset` | Label stored with results for provenance. |
| `checkpoint_path` | `null` | `null` = use PANNs' default location (`~/panns_data/...`). Set an explicit path to pin the checkpoint file. |

### `audio` — decoding & windowing

| Key | Default | Meaning |
|-----|---------|---------|
| `sample_rate` ⚠️ | `32000` | PANNs' required input rate. Do **not** change — other values break detection quality. |
| `window_seconds` 🔧 | `60` | Streaming window size. Larger = fewer model calls (slightly faster) but more RAM per window. |
| `min_window_seconds` | `1.0` | Trailing windows shorter than this are skipped (too short for the model). |

### `normalization` — pre-detection gain (applied per window)

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` 🔧 | `true` | Turn normalization on/off. Off = raw levels (useful to prove detections don't depend on boosting). |
| `target_peak` 🔧 | `0.9` | Windows are scaled so their peak reaches this (0–1). |
| `max_gain` 🔧 | `20.0` | Ceiling on amplification (linear). Prevents over-boosting faint windows. |
| `noise_floor` 🔧 | `0.005` | If a window's peak is below this it's treated as silence and left untouched (stops noise being amplified into false positives). |

### `detection` — bark events

| Key | Default | Meaning |
|-----|---------|---------|
| `threshold` 🔧 | `0.15` | Frame score to count as a dog sound. **Lower = higher recall** (more, but noisier). Raise for higher precision. |
| `min_event_seconds` 🔧 | `0.15` | Discard events shorter than this. |
| `merge_gap_seconds` 🔧 | `0.4` | Merge two hot spans separated by less than this into one event. |
| `dog_classes` 🔧 | `Dog, Bark, Bow-wow, Yip, Howl, Growling, Whimper (dog)` | AudioSet class names counted as barking. Names must match PANNs spelling exactly. |

### `intensity` — per-event loudness

Loudness is measured from the **raw, un-normalized** audio at ~10 ms resolution.
The absolute value is stored; the `intensity_relative` (0–1) in `results.json` is
derived, with `1.0` = the loudest bark in scope. Each event also carries
`intensity_dbfs` (absolute), which stays comparable across files.

| Key | Default | Meaning |
|-----|---------|---------|
| `metric` 🔧 | `rms` | `rms` (perceived energy) or `peak` (max amplitude within the bark). |
| `scope` 🔧 | `per_file` | `per_file` = loudest bark in *each* recording is 1.0; `global` = loudest bark across *all* recordings is 1.0 (comparable between files). |

> Note: intensity reflects what the microphone captured (distance, mic gain, and
> the H6's limiter all affect it), not the dog's true loudness. In `per_file`
> scope even a quiet night's loudest bark reads 1.0 — use `intensity_dbfs` for
> absolute comparison.

### `ingest` — copy & identity

| Key | Default | Meaning |
|-----|---------|---------|
| `file_extensions` | `[".mp3"]` | Which files to pick up from `source`. |
| `timestamp_source_label` | `sdcard_ctime` | Recorded with each file to document how its start time was derived. |
| `hash_prefix_len` | `12` | Length of the SHA-256 prefix used in archive filenames. |
| `archive_name_template` | `{start}_{hash}_{name}` | Archive filename pattern. Tokens: `{start}`=recording start (local, `yymmdd_hhmm`), `{hash}`=short sha, `{name}`=original name. |
| `hash_chunk_bytes` | `1048576` | Read block size when hashing (memory/speed tradeoff). |

### `snippets` — per-event clips

| Key | Default | Meaning |
|-----|---------|---------|
| `padding_seconds` 🔧 | `2.0` | Audio context added before **and** after each event. |
| `quality` | `5` | `libmp3lame` VBR quality (0 = best/large … 9 = worst/small). |
| `codec` | `libmp3lame` | ffmpeg audio codec for the clip. |
| `channels` | `1` | Snippet channel count (mono). |
| `extension` | `mp3` | Clip file extension. |
| `normalize` 🔧 | `true` | Loudness-normalize clips so faint barks are audible on review. Only affects the listening clips — the archived original is never modified. Recorded in `results.json` provenance. |
| `normalize_target_lufs` 🔧 | `-16.0` | EBU R128 integrated loudness target for `loudnorm` (higher = louder). |

### `coverage` — timeline & gaps

| Key | Default | Meaning |
|-----|---------|---------|
| `merge_gap_seconds` 🔧 | `5.0` | Recordings within this gap are treated as contiguous (so H6 file-splits don't look like gaps). |
| `night_start_hour` 🔧 | `22` | Local hour the "night" window begins (for night bark counts). |
| `night_end_hour` 🔧 | `6` | Local hour the night window ends. |

### `export`

| Key | Default | Meaning |
|-----|---------|---------|
| `filename` | `results.json` | Output file written into `export_dir`. |

### `logging`

| Key | Default | Meaning |
|-----|---------|---------|
| `level` 🔧 | `INFO` | `DEBUG` \| `INFO` \| `WARNING`. `DEBUG` adds a per-window position line. |
| `progress_bar` 🔧 | `true` | Live tqdm bar per file. Set `false` for headless/cron/redirected runs. |
| `log_file` | `data/processing.log` | Appended, timestamped audit trail. `null` = console only. |

## Tests

Pure logic (no ffmpeg/model needed):

```bash
python -m pytest tests/ -v
```

## Timestamp note (chain of custody)

Recording start is taken from the SD card's FAT creation time, which the H6
writes in local wall-clock. This assumes the recorder's clock and this PC share
the `timezone` set in `config.yml`. The card's modified time is also stored
(`mtime_utc`) as a cross-check. Because the timestamp lives on the card, always
run `ingest` against the card itself, not a manual copy.
```
