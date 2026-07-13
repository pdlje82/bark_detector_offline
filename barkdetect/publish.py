"""Assemble a self-contained static web bundle for hosting.

The bundle (config `paths.site_dir`, e.g. data/site) contains ONLY what should
be public — the frontend, the exported results.json, and the snippet clips.
The original recordings (archive/), the database, and logs are never included.
This bundle is the single deploy unit: serve it locally or upload it as-is.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)


def _is_junk(rel: Path) -> bool:
    """True if any path part is hidden (dotfile) or a cache dir — never publish these."""
    return any(part.startswith(".") or part == "__pycache__" for part in rel.parts)


def _copy_if_changed(src: Path, dst: Path) -> bool:
    """Copy src->dst if dst is missing or a different size; return True if copied."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return False
    shutil.copy2(src, dst)
    return True


def publish(cfg, store: Store) -> dict:
    """Build the static site bundle: index.html + results.json + snippets/.

    Frontend assets come from the repo's `frontend/` dir; the results file and
    snippets come from the pipeline's data dirs. Snippets are synced
    incrementally (only new/changed files are copied).
    """
    site = cfg.path("site_dir")
    frontend = cfg.project_root / "frontend"
    export_file = cfg.path("export_dir") / cfg.export.filename
    snippets = cfg.path("snippets_dir")

    if not frontend.exists():
        raise SystemExit(f"frontend dir not found: {frontend}")
    if not export_file.exists():
        raise SystemExit(
            f"results file not found: {export_file}. Run the 'export' step first.")

    site.mkdir(parents=True, exist_ok=True)

    # 1) Frontend assets — everything under frontend/ except any sample
    #    results.json the designer may have shipped (the real one is copied next).
    n_assets = 0
    for f in frontend.rglob("*"):
        rel = f.relative_to(frontend)
        if f.is_file() and f.name != cfg.export.filename and not _is_junk(rel):
            _copy_if_changed(f, site / rel)
            n_assets += 1

    # 2) The real exported results file.
    shutil.copy2(export_file, site / cfg.export.filename)

    # 3) Snippets, co-located as ./snippets/ so snippet_url resolves. Incremental.
    n_snip = 0
    if snippets.exists():
        for f in snippets.rglob("*"):
            if f.is_file() and _copy_if_changed(f, site / "snippets" / f.relative_to(snippets)):
                n_snip += 1

    log.info("  published to %s  (%d frontend files, %d new/updated snippets)",
             site, n_assets, n_snip)
    return {"site_dir": str(site), "frontend_files": n_assets, "snippets_copied": n_snip}
