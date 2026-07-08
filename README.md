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

## Setup (conda / mamba)

```bash
mamba env create -f environment.yml
mamba activate bark-detector-offline
# quick check
python -c "import torch, panns_inference, librosa, soundfile; print('ok')"
```

`ffmpeg` is installed into the env by conda — no separate install needed. The
PANNs model checkpoint (~300 MB) downloads automatically on first `analyze`.

Edit `config.yml` before first use — at minimum set `timezone` to match the
recorder's clock, and confirm the detection `threshold`.

## Usage

Every few days, plug in the SD card and run one command (replace `E:\` with the
card's drive letter):

```bash
python -m barkdetect run --source E:\
```

Or step by step:

```bash
python -m barkdetect ingest  --source E:\   # copy + register new files
python -m barkdetect analyze                # detect barks (only unprocessed files)
python -m barkdetect export                 # regenerate results.json
```

Re-running is safe: files already ingested (same SHA-256) are skipped.

## Output layout

```
data/
  archive/            immutable copies of the original MP3s
  snippets/<hash>/    per-event .mp3 clips (referenced by results.json)
  barks.db            SQLite source of truth
  export/results.json frontend input
```

`results.json` contains: `recordings`, `coverage`, `gaps`, `daily_summary`,
and `events` (each with `abs_start_local`, `class`, `peak_conf`, `night`,
`snippet_url`). Serve `data/export/` and `data/snippets/` as static files to the
frontend.

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
