"""Temporary demo allowlist from Health Net website cross-check results.

The CSV is deliberately an input roster, not a source of rate values.  Its
"rate_file_ref" field is a provider-group reference ID, while prices must
still come from the actual Transparency-in-Coverage rate file.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

DEFAULT_DEMO_CSV = Path("data/ppo_check_results.csv")
PPO_VERDICTS = {
    "FOUND_PPO_BY_NPI",
    "FOUND_PPO_SAME_FACILITY_DIFFERENT_NPI",
    "NOT_FOUND_BY_NPI_SAME_TIN_OTHER_SITES_PPO",
}
ALL_VERDICTS = PPO_VERDICTS | {"NOT_FOUND"}


def _digits(value) -> str:
    return re.sub(r"\\D", "", str(value or "").split(".")[0])


def load_demo_rows(path: str | Path = DEFAULT_DEMO_CSV) -> list[dict]:
    """Read/validate the reviewed demo rows, preserving NPIs and TINs as strings."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Demo roster not found: {path}. Copy ppo_check_results.csv to data/ppo_check_results.csv."
        )
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    required = {"npi", "rate_file_tin", "verdict"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path} must have columns: {', '.join(sorted(required))}")
    seen = set()
    clean = []
    for row in rows:
        npi, tin = _digits(row.get("npi")), _digits(row.get("rate_file_tin"))
        verdict = str(row.get("verdict") or "").strip().upper()
        if len(npi) != 10:
            continue
        if verdict not in ALL_VERDICTS:
            raise ValueError(f"Unexpected demo verdict {verdict!r} for NPI {npi}")
        key = (npi, tin)
        if key in seen:
            continue
        seen.add(key)
        row["npi"] = npi
        row["rate_file_tin"] = tin
        row["verdict"] = verdict
        clean.append(row)
    if not clean:
        raise ValueError(f"No valid NPI/TIN rows found in {path}")
    return clean


def demo_pairs(path: str | Path = DEFAULT_DEMO_CSV) -> list[tuple[int, str]]:
    return [(int(r["npi"]), r["rate_file_tin"]) for r in load_demo_rows(path) if r["rate_file_tin"]]


def demo_pair_status(path: str | Path = DEFAULT_DEMO_CSV) -> dict[tuple[int, str], str]:
    """Return pair-specific directory verdicts; an explicit NOT_FOUND is not PPO evidence."""
    out = {}
    for row in load_demo_rows(path):
        if not row["rate_file_tin"]:
            continue
        out[(int(row["npi"]), row["rate_file_tin"])] = (
            "CONFIRMED_PPO" if row["verdict"] in PPO_VERDICTS else "UNRESOLVED"
        )
    return out
