"""Load and expose config.yml as a nested attribute object."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml


def _to_ns(obj):
    """Recursively turn dicts into SimpleNamespace for attribute access."""
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _to_ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_to_ns(v) for v in obj]
    return obj


class Config:
    def __init__(self, data: dict, project_root: Path):
        self._raw = data
        self.project_root = project_root
        self.timezone = data["timezone"]
        self.audio = _to_ns(data["audio"])
        self.normalization = _to_ns(data["normalization"])
        self.detection = _to_ns(data["detection"])
        self.snippets = _to_ns(data["snippets"])
        self.coverage = _to_ns(data["coverage"])
        self._paths = data["paths"]

    def path(self, key: str) -> Path:
        """Resolve a configured path relative to the project root."""
        p = Path(self._paths[key])
        return p if p.is_absolute() else (self.project_root / p)

    @classmethod
    def load(cls, config_path: str | Path = "config.yml") -> "Config":
        config_path = Path(config_path).resolve()
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(data, project_root=config_path.parent)
