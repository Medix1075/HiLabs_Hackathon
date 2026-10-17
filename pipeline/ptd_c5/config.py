"""Load the C5 YAML config."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "c5.yaml"


def load_config(path: str | Path | None = None, as_of: str | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_path"] = str(path)
    as_of = as_of or cfg.get("as_of")
    cfg["_as_of"] = date.fromisoformat(str(as_of)) if as_of else datetime.now(timezone.utc).date()
    return cfg
