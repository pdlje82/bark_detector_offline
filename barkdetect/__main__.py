"""Entry point for `python -m barkdetect` — runs the config-driven pipeline."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
