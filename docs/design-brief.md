# Frontend design brief (prompt for Claude)

Paste this into claude.ai (Artifacts) to prototype the frontend, **and paste the
full contents of `sample_results.json` where indicated**. The prototype must
embed the sample data inline (claude.ai Artifacts cannot fetch local files); the
hostable version will instead `fetch('./results.json')`.

---

## Prompt

Build a single-page web app that presents dog-barking evidence for a noise
complaint. It reads one JSON file, `results.json`, produced by an offline
analysis pipeline, plus audio clips it references. This will be used as evidence
for police/lawyers, so the tone must be **sober, factual, and trustworthy** — not
flashy. Accuracy and clarity over decoration.

**Tech constraints**
- A single self-contained `index.html` (vanilla HTML/CSS/JS, no build step, no
  external CDNs — inline everything). It must run when served as static files.
- Data source: in this prototype, use the embedded `RESULTS` object below. In
  production it will be replaced by `const RESULTS = await (await fetch('./results.json')).json()`.
- Audio: play each event's `snippet_url` with a native `<audio>` element. In the
  prototype the clips won't exist — degrade gracefully (disabled play button).
- Responsive; works offline; light/dark friendly.

**The data**
The schema is described below; here is a real example to build against:

```json
<PASTE THE FULL CONTENTS OF sample_results.json HERE>
```

Field reference (see `results-schema.md` for full detail):
- `timezone`, `generated_at`, `parameters` (settings used, for a provenance panel).
- `recordings[]`: `original_filename`, `sha256`, `start_local`, `duration_sec`.
- `coverage[]` / `gaps[]`: `{start, end, duration_sec}` — recorded spans vs. spans
  with NO recording. All local time.
- `daily_summary[]`: `date`, `count`, `total_bark_seconds`, `night_count`.
- `events[]`: `abs_start_local`, `class` (Bark/Howl/Yip/…), `duration_sec`,
  `peak_conf`/`mean_conf` (0..1 detection confidence), `night` (bool),
  `intensity_relative` (0..1, 1=loudest bark), `intensity_dbfs` (absolute
  loudness), `snippet_url` (audio clip). Confidence ≠ loudness — show both.

**Views (in priority order)**
1. **Header summary** — date range covered, total events, night-time events,
   number of recordings, and total recorded time vs. total gap time (a coverage
   figure). One glance = the headline.
2. **Daily chart** — a bar per `daily_summary` day, split day vs. night counts.
   The pattern over days is the core argument.
3. **Day detail: 24-hour timeline** — pick a day; show a horizontal 00:00–24:00
   strip where **recorded time is solid and gaps are hatched/greyed** (from
   `coverage`/`gaps`), with each bark as a tick placed at its `abs_start_local`,
   colored by `intensity_relative`. Clicking a tick opens/plays that event.
4. **Event table** — sortable, filterable rows: time, class, duration, confidence,
   `intensity_dbfs`, night flag, and an inline **play** button for `snippet_url`.
   Filters: night-only, min confidence, min intensity, by class, by day.
5. **Provenance panel** — render `parameters` (model, threshold, normalization,
   whether clips were loudness-boosted), recording hashes, and timestamp source.
   This is what makes it defensible; keep it accessible but out of the way.

**Important honesty rules**
- Never render a gap as "quiet" — label it "no recording".
- Keep `intensity_relative` (ranking) and `intensity_dbfs` (absolute) distinct;
  note that per-file relative resets per recording.
- Show `generated_at` and `schema_version` somewhere small.

Deliver one `index.html`. Start simple and correct; we'll iterate on styling.

---

## Training mode (schema v2 — per-dog labeling)

Paste this as a follow-up once the base app exists. It adds a labeling workflow
so residents can teach the system to tell the dogs apart.

> Add a **Training mode** to `index.html`, **off by default**, toggled by a switch
> in the header. When off, all existing views are unchanged. Keep the page fully
> static — the only network call remains `fetch('./results.json')`.
>
> **Roster:** read `RESULTS.dogs` (array of dog names) for the label options.
> Always add three fixed options: `Unsure`, `Multiple dogs`, `Not a dog` (false
> positive). The stored label values are the roster names verbatim, or
> `unsure` / `multiple` / `not_a_dog`.
>
> **Labeling (training mode on):** each event in the table/detail gets a labeling
> control (dropdown or button group) beside its play button. Selecting a value
> records `{ [event.key]: label }` — use `event.key` (the stable id), never
> `event.id`. Persist all labels in `localStorage` so they survive reloads. Show
> the current label per row; provide a "show unlabeled only" filter and
> keyboard shortcuts (play, then a key per dog) to label quickly.
>
> **Export:** an "Export labels" button downloads `labels.json`:
> ```json
> { "schema": 1, "exported_at": "<ISO>",
>   "labels": { "<event.key.a>": "rex", "<event.key.b>": "not_a_dog" } }
> ```
>
> **Predictions (both modes):** show `dog_label` with `dog_confidence`, clearly
> distinguishing `dog_label_source === "human"` (confirmed — solid styling) from
> `"predicted"` (a muted "suggested" tag). Never present a prediction as fact.
> Optionally add a per-dog view driven by `daily_summary[].by_dog`.
>
> Conform to `docs/results-schema.md` (v2); use only fields defined there. Do not
> touch backend files.

## After the prototype

When the look is right, ask for the production version: identical UI but with the
embedded `RESULTS` replaced by a `fetch('./results.json')`, plus a graceful
"couldn't load results.json" message. That file drops into `frontend/` and is
served next to `data/export/results.json` + `data/snippets/`.
