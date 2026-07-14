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
from datetime import datetime, timezone

from .identify import _canonical_label, _label_to_list
from .store import Store

log = logging.getLogger(__name__)


def _build_app(cfg):
    """Construct the Flask app (imported lazily so the rest of the CLI needs no Flask)."""
    from flask import Flask, jsonify, request, send_from_directory

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
