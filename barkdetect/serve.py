"""Local label server: the frontend reads/writes human labels directly in the DB.

Run with `python -m barkdetect serve`. Serves the frontend, the exported
results.json, and the snippet clips, plus a small label API backed by the same
barks.db the pipeline uses. Binds to localhost by default (config `serve.*`), so
labeling is a local, single-user tool. The hosted, read-only evidence view stays
the static `publish` bundle — it has no API and shows labels baked into
results.json.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone

from .enhance import enhanced_path, filter_string
from .identify import _canonical_label, _label_to_list
from .store import Store

log = logging.getLogger(__name__)


def _build_app(cfg):
    """Construct the Flask app (imported lazily so the rest of the CLI needs no Flask)."""
    from flask import Flask, Response, jsonify, request, send_from_directory

    frontend = cfg.project_root / "frontend"
    export_dir = cfg.path("export_dir")
    snippets_dir = cfg.path("snippets_dir")
    db_path = str(cfg.path("db_path"))

    app = Flask(__name__)

    # --- static site ---
    @app.get("/")
    def index():
        return send_from_directory(frontend, "index.html")

    @app.get("/results.json")
    def results():
        return send_from_directory(export_dir, cfg.export.filename)

    @app.get("/snippets/<path:relpath>")
    def snippet(relpath):
        return send_from_directory(snippets_dir, relpath)

    @app.get("/<path:asset>")
    def frontend_asset(asset):
        return send_from_directory(frontend, asset)

    # --- label API (source of truth = barks.db) ---
    @app.get("/api/labels")
    def get_labels():
        with Store(db_path) as store:
            raw = store.all_labels()
        # return multi-dog labels as arrays, single as strings
        return jsonify({k: (_label_to_list(v) if v and v.startswith("[") else v)
                        for k, v in raw.items()})

    @app.put("/api/labels/<event_key>")
    def put_label(event_key):
        value = (request.get_json(silent=True) or {}).get("label")
        if value in (None, "", []):
            return jsonify({"error": "missing 'label'"}), 400
        now = datetime.now(timezone.utc).isoformat()
        with Store(db_path) as store:
            store.upsert_label(event_key, _canonical_label(value), "human", now)
            store.commit()
        return jsonify({"ok": True, "event_key": event_key})

    @app.delete("/api/labels/<event_key>")
    def delete_label(event_key):
        with Store(db_path) as store:
            deleted = store.delete_label(event_key)
        return jsonify({"ok": True, "deleted": deleted})

    # --- continuous region audio (for burst playback; labeling mode only) ---
    @app.get("/api/audio/<sha>")
    def audio_region(sha):
        """Stream a continuous [start, start+dur] cut of a recording's raw audio.

        Lets the frontend play a whole burst (or any span) as one real segment,
        instead of concatenating padded per-bark clips.
        """
        try:
            start = float(request.args.get("start", "0"))
            dur = float(request.args.get("dur", "0"))
        except ValueError:
            return jsonify({"error": "start/dur must be numbers"}), 400
        if start < 0 or dur <= 0 or dur > 120:
            return jsonify({"error": "start >= 0 and 0 < dur <= 120 required"}), 400
        source = request.args.get("source", "raw")
        with Store(db_path) as store:
            rec = store.recording_by_sha_prefix(sha)
        if not rec:
            return jsonify({"error": "unknown recording"}), 404

        # source=enhanced: read the precomputed enhanced copy; if it's missing,
        # apply the enhancement chain on the fly from raw. source=raw: the original.
        input_path, extra_af = rec["archived_path"], ""
        if source == "enhanced":
            ep = enhanced_path(cfg, rec["archived_path"])
            if ep.exists():
                input_path = str(ep)
            else:
                extra_af = filter_string(cfg.enhancement.chain)

        cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", input_path, "-t", f"{dur:.3f}"]
        if extra_af:
            cmd += ["-af", extra_af]
        cmd += ["-ac", "1", "-c:a", "libmp3lame", "-q:a", "5", "-f", "mp3", "-"]
        out = subprocess.run(cmd, capture_output=True)
        if out.returncode != 0 or not out.stdout:
            return jsonify({"error": "audio cut failed"}), 500
        resp = Response(out.stdout, mimetype="audio/mpeg")
        # (sha,start,dur,source) fully determines the bytes -> cache aggressively
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    return app


def serve(cfg, store: Store) -> dict:
    """Run the label server (blocking) until interrupted.

    `store` is accepted for a uniform step signature but not used — the server
    opens its own short-lived connections per request.
    """
    host = getattr(cfg.serve, "host", "127.0.0.1")
    port = int(getattr(cfg.serve, "port", 8000))
    app = _build_app(cfg)
    log.info("  labeling server on http://%s:%d  (Ctrl+C to stop)", host, port)
    log.info("  labels are saved directly to %s", cfg.path("db_path"))
    app.run(host=host, port=port, threaded=True)
    return {"served": True}
