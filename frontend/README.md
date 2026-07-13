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

## Behaviour notes

- **Missing / unreadable data** — if `results.json` cannot be fetched or parsed,
  the page shows a friendly "Couldn't load the evidence" message with a retry
  button instead of a blank screen.
- **Missing audio** — if a clip referenced by `snippet_url` is absent, its play
  control degrades gracefully and the event shows a "Clip unavailable" note.
- **Schema version** — the page is built for `schema_version: 1`. If a file
  reports a different version it still renders, but a non-blocking warning banner
  is shown so consumers know some fields may not display correctly.
- **Honesty** — gaps between recordings are drawn explicitly and labelled
  "no recording" (never shown as quiet). Detection **confidence** and
  **loudness** (`dbfs` / relative intensity) are kept as separate columns;
  relative intensity resets per file in `per_file` scope.
- **Responsive & theme** — works on mobile (the event table collapses to cards)
  and desktop, and follows the OS light/dark preference.
