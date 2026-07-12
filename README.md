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

**5. Absolute timing, snippets, provenance.** Event offsets are added to the
recording's start time to get absolute UTC/local timestamps. A padded MP3 clip is
cut per event for later listening, and the exact parameter set used is stored with
the recording (see `parameters` in `results.json`) so results are reproducible.

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
`night`, `snippet_url`). Each recording also carries the exact `parameters` that
produced its events. Serve `data/export/` and `data/snippets/` as static files
to the frontend.

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
