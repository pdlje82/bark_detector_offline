"""Config-driven entry point. No command-line arguments.

Reads `run.steps` from the config and executes each stage in order. Configure
which steps run, the source path, and every parameter in config.yml.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .analyze import analyze
from .config import Config, setup_logging
from .export import export
from .ingest import ingest
from .publish import publish
from .store import Store

log = logging.getLogger(__name__)

# step name -> callable(cfg, store)
STEP_FUNCS = {
    "ingest": ingest,
    "analyze": analyze,
    "export": export,
    "publish": publish,
}


def _validate(cfg) -> list[str]:
    """Validate run.steps and (for ingest) run.source; return the step list or exit."""
    steps = list(cfg.run.steps or [])
    if not steps:
        raise SystemExit("Nothing to do: 'run.steps' is empty in the config.")

    unknown = [s for s in steps if s not in STEP_FUNCS]
    if unknown:
        raise SystemExit(
            f"Unknown step(s) {unknown}. Valid steps: {sorted(STEP_FUNCS)}."
        )

    if "ingest" in steps:
        src = getattr(cfg.run, "source", None)
        if not src:
            raise SystemExit("Step 'ingest' requires 'run.source' to be set.")
        if not Path(src).exists():
            raise SystemExit(f"Ingest source does not exist: {src}")

    return steps


def main():
    """Load config, set up logging, and run the configured steps in order."""
    cfg = Config.resolve()
    setup_logging(cfg)
    steps = _validate(cfg)

    store = Store(cfg.path("db_path"))
    with store:
        for i, step in enumerate(steps, 1):
            log.info("[%d/%d] %s ...", i, len(steps), step)
            STEP_FUNCS[step](cfg, store)
    log.info("All done.")


if __name__ == "__main__":
    sys.exit(main())
