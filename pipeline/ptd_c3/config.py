"""Load the C3 YAML config and pre-compile its regexes."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "c3.yaml"


def load_config(path: str | Path | None = None, as_of: str | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_path"] = str(path)
    as_of = as_of or cfg.get("as_of")
    cfg["_as_of"] = date.fromisoformat(str(as_of)) if as_of else datetime.now(timezone.utc).date()
    cfg["_code_formats"] = {t.upper(): re.compile(p) for t, p in cfg["code_formats"].items()}
    cfg["_catch_all_patterns"] = [re.compile(p, re.IGNORECASE) for p in cfg["junk"]["catch_all_code_patterns"]]
    lower = lambda xs: {str(x).lower() for x in xs}  # noqa: E731
    r = cfg["rates"]
    r["_dollar"], r["_per_diem"], r["_percentage"] = lower(r["dollar_types"]), lower(r["per_diem_types"]), lower(r["percentage_types"])
    s = cfg["schema"]
    for k in ("arrangements", "negotiated_types", "billing_classes", "settings", "tin_types"):
        s["_" + k] = lower(s[k])
    s["_billing_code_types"] = {str(x).upper() for x in s["billing_code_types"]}
    cfg["junk"]["_placeholder_tins"] = {str(t) for t in cfg["junk"]["placeholder_tins"]}
    cfg["junk"]["_catch_all_types"] = {str(t).upper() for t in cfg["junk"]["catch_all_code_types"]}
    return cfg
