"""Load and expose config.yml as a nested attribute object.

The pipeline is entirely config-driven: there are no command-line arguments.
The config file is `config.yml` in the current directory, overridable via the
BARKDETECT_CONFIG environment variable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import yaml

DEFAULT_CONFIG = "config.yml"
CONFIG_ENV_VAR = "BARKDETECT_CONFIG"


def _to_ns(obj):
    """Recursively turn dicts into SimpleNamespace for attribute access."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_ns(v) for v in obj]
    return obj


class Config:
    """Parsed configuration, exposing each config.yml section as attributes.

    Sections are available as nested attribute objects (e.g. `cfg.detection.threshold`);
    `path()`/`resolve_path()` resolve data paths and `params_snapshot()` captures the
    result-affecting settings for provenance.
    """

    def __init__(self, data: dict, project_root: Path):
        """Build a Config from a parsed YAML dict rooted at `project_root`."""
        self._raw = data
        self.project_root = project_root
        self.timezone = data["timezone"]
        self.run = _to_ns(data["run"])
        self.model = _to_ns(data["model"])
        self.audio = _to_ns(data["audio"])
        self.normalization = _to_ns(data["normalization"])
        self.detection = _to_ns(data["detection"])
        self.intensity = _to_ns(data["intensity"])
        self.onset = _to_ns(data.get("onset", {
            "use_onset_detection": False, "min_interval_seconds": 0.12,
            "delta": 0.07, "debug_plots": False,
            "debug_plots_dir": "data/onset_debug", "debug_plots_max": 150,
        }))
        self.identification = _to_ns(data["identification"])
        self.enhancement = _to_ns(data.get("enhancement", {
            "enabled": False, "format": "mp3", "dir": "data/enhanced",
            "apply_to": ["listen"], "chain": [],
        }))
        self.ingest = _to_ns(data["ingest"])
        self.snippets = _to_ns(data["snippets"])
        self.coverage = _to_ns(data["coverage"])
        self.export = _to_ns(data["export"])
        self.serve = _to_ns(data.get("serve", {"host": "127.0.0.1", "port": 8000}))
        self.logging = _to_ns(data["logging"])
        self._paths = data["paths"]

    @property
    def base_dir(self) -> Path:
        """Base directory that relative data paths resolve against.

        Defaults to the config file's directory (the repo). Set `paths.root`
        to keep data (archive, db, snippets, export) outside the git checkout.
        """
        root = self._paths.get("root")
        if root:
            rp = Path(root)
            return rp if rp.is_absolute() else (self.project_root / rp)
        return self.project_root

    def resolve_path(self, p: str | Path) -> Path:
        """Resolve a path against base_dir (absolute paths pass through)."""
        p = Path(p)
        return p if p.is_absolute() else (self.base_dir / p)

    def path(self, key: str) -> Path:
        """Resolve a configured data path against base_dir."""
        return self.resolve_path(self._paths[key])

    def params_snapshot(self) -> dict:
        """The parameters that materially affect detection results.

        Persisted per recording and echoed in results.json so any exported
        result is reproducible to the exact settings that produced it.
        """
        return {
            "model": {
                "name": self.model.name,
                "version": self.model.version,
                "device": self.model.device,
            },
            "normalization": dict(vars(self.normalization)),
            "detection": dict(vars(self.detection)),
            "audio": {
                "sample_rate": self.audio.sample_rate,
                "window_seconds": self.audio.window_seconds,
            },
            "intensity": {
                "metric": self.intensity.metric,
                "scope": self.intensity.scope,
            },
            "onset": {
                "use_onset_detection": self.onset.use_onset_detection,
                "min_interval_seconds": self.onset.min_interval_seconds,
                "delta": self.onset.delta,
            },
            "enhancement": {
                k: self._raw.get("enhancement", {}).get(k)
                for k in ("enabled", "format", "apply_to", "chain")
            },
            "identification": {
                "enabled": self.identification.enabled,
                "embedding": self.identification.embedding,
                "classifier": self.identification.classifier,
                "dogs": list(self.identification.dogs),
            },
            "snippets": {
                # documents whether listening clips were loudness-boosted
                "normalized": getattr(self.snippets, "normalize", False),
                "target_lufs": getattr(self.snippets, "normalize_target_lufs", None),
                # default context padding for a single-bark clip (frontend seed)
                "padding_seconds": getattr(self.snippets, "padding_seconds", 0.5),
            },
        }

    @classmethod
    def load(cls, config_path: str | Path = DEFAULT_CONFIG) -> "Config":
        """Load and parse a config.yml from an explicit path."""
        config_path = Path(config_path).resolve()
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(data, project_root=config_path.parent)

    @classmethod
    def resolve(cls) -> "Config":
        """Load the active config, honoring the BARKDETECT_CONFIG env var."""
        return cls.load(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG))


def setup_logging(cfg: Config) -> None:
    """Configure the root logger from config: console + optional audit file."""
    level = getattr(logging, str(cfg.logging.level).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    log_file = getattr(cfg.logging, "log_file", None)
    if log_file:
        path = cfg.resolve_path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")   # append across runs
        fh.setFormatter(fmt)
        root.addHandler(fh)
