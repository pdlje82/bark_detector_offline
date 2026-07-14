# Evidence frontend

A single self-contained page that presents dog-barking detection results. All
CSS and JS are inline in `index.html`; there is **no build step**, no bundler,
and no external/CDN dependencies.

## Serving requirement

The page loads its data at runtime with `fetch('./results.json')`, so it **must
be served over HTTP** — opening `index.html` directly from disk (`file://`)
will fail because browsers block `fetch` on the `file://` scheme.

At serve time, three things must sit **side by side as siblings** in the same
served directory:

```
<served dir>/
├── index.html        # this page
├── results.json      # the exported results (data contract: docs/results-schema.md)
└── snippets/         # the audio clips referenced by each event's snippet_url
    └── …
```

`results.json` and the `snippets/` folder are assembled next to `index.html` by
the backend's publish step. Each event's `snippet_url` is a path **relative to
this page** (e.g. `snippets/<hash>/<name>.mp3`), so as long as the three live
together, audio playback resolves with no configuration.

Do not change the `fetch('./results.json')` call or the relative snippet paths —
they are correct for this co-located layout.

## Running locally

Serve the folder over HTTP from any static server, for example:

```
# Python
python -m http.server -d <served dir> 8000

# Node
npx serve <served dir>
```

Then open `http://localhost:8000/`.

A copy of the sample results ships here as `results.json` for local testing;
in production it is replaced by the real export.

## Labeling mode & the label API

The page detects its mode at load by probing the label API with `GET api/labels`
(relative to the page, same origin as the static assets and `results.json`):

- **API reachable → Labeling mode.** Labels are read from the API on load and the
  API is the source of truth (a small in-memory cache is kept only for
  responsiveness). Every event gets a multi-select chip control; setting or
  changing a label immediately `PUT api/labels/<event.key>` with
  `{ "label": <string|array> }`, and clearing it sends `DELETE
  api/labels/<event.key>`. Writes are optimistic — the change shows at once; if
  the request fails a brief "couldn't save — retry" note appears while the
  pending change stays visible, and Retry re-sends it. A header indicator reads
  **"Labeling — saved to database."**
- **API not reachable → Read-only presentation mode.** No labeling controls; the
  page shows the labels/predictions already in `results.json`
  (`dog_labels` / `dog_label` / `dog_label_source`) only. The indicator reads
  **"Read-only."**

- **Roster** — label options come from `RESULTS.dogs`, plus three fixed extras:
  *Unsure*, *Multiple dogs*, and *Not a dog (false positive)*.
- **Labeling** — each event in the table and detail gets a multi-select label
  control (chips) next to its play button. Dog names (from `RESULTS.dogs`) are
  multi-toggle — tag two dogs when two are barking — while the three specials
  (Unsure / Multiple dogs / Not a dog) are mutually exclusive and clear any dog
  selection (and vice-versa). The value sent to the API is a **string** for a
  single dog/option or an **array** of names for several dogs.
- **Status at a glance** — every event shows a status dot in both modes (in the
  table, the detail card, the 24h timeline ticks, and the calendar): *unlabeled*
  (hollow), *confirmed* = the human label matches the model suggestion (green),
  or *relabeled* = the human label differs from the suggestion (amber). The
  header shows running `confirmed · relabeled · unlabeled` counts, and the
  calendar marks each day as fully or partly reviewed.
- **Filters & sort** — a Status dropdown (All / Labeled / Unlabeled / Confirmed /
  Relabeled), a Dog dropdown (filter by the assigned label — combines with
  status), and a Sort control (Time / Confidence / Intensity / Duration / Class,
  with a direction toggle). All event filters and the sort apply to **both** the
  event log and the day-detail timeline + "events in window" list.
- **Speed** — keyboard shortcuts (labeling mode): `1`–`9` toggle a roster
  option, `0` clears, `Space` plays/pauses the current clip, `J`/`K` (or
  `↓`/`↑`) step through events, and `N` jumps to the next unlabeled.

The `event.key` is the stable identifier used both as the API path segment and
the map key. All API paths are relative, so the same file works wherever the
backend serves it.

## Dog identification reliability

When the pipeline has trained a dog classifier, `results.json` carries an
`identification_metrics` object; a panel surfaces it:

- **Not present (`null`)** — shows "No dog model trained yet — label more clips
  to enable it."
- **`trained: false`** — shows the `reason` and per-dog `label_counts` progress
  (e.g. "Cooper: 4 / 8 needed") so you can see which dogs still need labels.
- **`trained: true`** — a headline cross-validated accuracy (`accuracy`,
  `cv_folds`), stated as an estimate for the model's *suggested* predictions only
  (human-confirmed labels are taken as given); a confusion-matrix heatmap (rows =
  true dog, columns = predicted, diagonal = correct in green, confusions in red);
  and a per-dog precision / recall / F1 / support table.

Per-event `dog_confidence` still shows next to a predicted label in the log
(e.g. "Rex · 82%"), muted, with a tooltip noting it is a raw, over-confident
score — the panel's cross-validated accuracy is the reliable figure.

## Behaviour notes

- **Missing / unreadable data** — if `results.json` cannot be fetched or parsed,
  the page shows a friendly "Couldn't load the evidence" message with a retry
  button instead of a blank screen.
- **Missing audio** — if a clip referenced by `snippet_url` is absent, its play
  control degrades gracefully and the event shows a "Clip unavailable" note.
- **Schema version** — the page supports `schema_version` 1 and 2. If a file
  reports a different version it still renders, but a non-blocking warning banner
  is shown so consumers know some fields may not display correctly.
- **Predictions vs. confirmations** — when present, each event's `dog_label` /
  `dog_confidence` is shown in both modes. A `dog_label_source` of `"human"` is
  styled as confirmed; `"predicted"` is shown muted with a "suggested" tag. A
  prediction is never presented as confirmed.
- **Honesty** — gaps between recordings are drawn explicitly and labelled
  "no recording" (never shown as quiet). Detection **confidence** and
  **loudness** (`dbfs` / relative intensity) are kept as separate columns;
  relative intensity resets per file in `per_file` scope.
- **Responsive & theme** — works on mobile (the event table collapses to cards)
  and desktop, and follows the OS light/dark preference.
