"""Inputs the team keeps in the repo, so a run never has to ask for them.

pipeline/inputs/ holds two files that are not downloaded by the pipeline:

  Rate Benchmarking Individual Weights_Hackathon.xlsx   HiLabs specialty -> billing code workbook
  c4_ppo_check.csv                                      saved Health Net PPO directory check (C4)

A path given on the command line always wins. Without the PPO check table, C4 searches the
Health Net directory live (`--check-directory`), which needs Playwright and can take hours.
"""
from __future__ import annotations

import csv
import importlib.util
import os
from pathlib import Path

INPUTS_DIR = "inputs"
WORKBOOK_NAME = "Rate Benchmarking Individual Weights_Hackathon.xlsx"
PPO_CHECK_NAME = "c4_ppo_check.csv"


def find_workbook(inputs: Path) -> Path | None:
    """The HiLabs workbook: the exact name first, then any other .xlsx in the folder (alphabetical)."""
    exact = inputs / WORKBOOK_NAME
    if exact.is_file():
        return exact
    others = sorted(p for p in inputs.glob("*.xlsx") if not p.name.startswith("~$"))
    return others[0] if others else None


def check_table_info(path: Path) -> tuple[str | None, int]:
    """(layout, NPIs searched) of a saved PPO check: 'c4' (c4_ppo_check.csv), 'legacy' (ptd_dir's early
    ppo_check_results.csv) or None (not a PPO check table). Mirrors ptd_c4.ppo_check.count_checked; kept
    free of pipeline imports so the UI can load this file on its own."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            head = fh.readline()
            fh.seek(0)
            rd = csv.DictReader(fh, delimiter="\t" if head.count("\t") > head.count(",") else ",")
            fields = set(rd.fieldnames or [])
            if {"npi", "site_checked", "verdict"} <= fields:
                layout, checked = "c4", lambda r: r.get("site_checked") == "yes"
            elif {"npi", "npi_search_result", "verdict"} <= fields:
                layout, checked = "legacy", lambda r: bool(r.get("npi_search_result"))
            else:
                return None, 0
            return layout, len({str(r.get("npi") or "").strip().split(".")[0] for r in rd if checked(r)} - {""})
    except (OSError, UnicodeError, csv.Error):
        return None, 0


def find_ppo_check(inputs: Path) -> Path | None:
    """The saved PPO directory check: c4_ppo_check.csv when it is a valid table, else the *ppo_check*.csv
    covering the most NPIs (c4 layout before the early ppo_check_results.csv layout)."""
    exact = inputs / PPO_CHECK_NAME
    if exact.is_file() and check_table_info(exact)[0]:
        return exact
    scored = []
    for p in sorted(inputs.glob("*ppo_check*.csv")):
        layout, n = check_table_info(p)
        if layout:
            scored.append((layout == "c4", n, p.name, p))
    return max(scored)[3] if scored else None


def live_check_available() -> tuple[bool, str]:
    """Whether C4 may search the Health Net directory itself when no saved check exists."""
    if os.environ.get("PTD_DIRECTORY_CHECK", "").lower() == "off":
        return False, "PTD_DIRECTORY_CHECK=off"
    if importlib.util.find_spec("playwright") is None:
        return False, "Playwright is not installed (pip install playwright; python -m playwright install chromium)"
    return True, ""


def discover(inputs: str | Path) -> dict:
    """What a run will use from the inputs folder, for the runner's log and the UI."""
    folder = Path(inputs)
    live, why_not = live_check_available()
    workbook, ppo = find_workbook(folder), find_ppo_check(folder)
    layout, checked = check_table_info(ppo) if ppo else (None, 0)
    ignored = [p.name for p in sorted(folder.glob("*.csv")) if p != ppo and not check_table_info(p)[0]]
    return {"inputs_dir": str(folder.resolve()), "workbook": str(workbook) if workbook else None,
            "ppo_check_csv": str(ppo) if ppo else None, "ppo_check_layout": layout, "ppo_check_npis": checked,
            "ignored_csv": ignored, "live_directory_check": ppo is None and live,
            "live_directory_check_unavailable": "" if live else why_not}
