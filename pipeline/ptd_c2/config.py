"""Load the YAML config and pre-compile its regexes."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "c2.yaml"


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_path"] = str(path)

    ns = cfg["name_signals"]
    ns["_keywords"] = {t: [_rx(p) for p in pats] for t, pats in ns["keywords"].items()}
    ns["_product_codes"] = {
        t: {code: _rx(p) for code, p in codes.items()} for t, codes in ns["product_codes"].items()
    }
    ns["_enhancedcare"] = [_rx(p) for p in ns["enhancedcare_codes"]]
    ns["_hints"] = {
        name: (h["type"], [_rx(p) for p in h["patterns"]]) for name, h in ns["hint_patterns"].items()
    }
    ns["_shapes"] = {name: (s["type"], _rx(s["pattern"])) for name, s in ns["benefit_shapes"].items()}

    probe = cfg["network_probe"]
    probe["_label_rules"] = [(r["label"], _rx(r["pattern"])) for r in probe["label_rules"]]
    return cfg
