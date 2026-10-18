"""Inputs the team keeps in the repo, so a run never has to ask for them.

pipeline/inputs/ holds two files that are not downloaded by the pipeline:

  Rate Benchmarking Individual Weights_Hackathon.xlsx   HiLabs specialty -> billing code workbook
  c4_ppo_check.csv                                      saved Health Net PPO directory check (C4)

A path given on the command line always wins. Without the PPO check table, C4 searches the
Health Net directory live (`--check-directory`), which needs Playwright and can take hours.
"""
from __future__ import annotations

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


def find_ppo_check(inputs: Path) -> Path | None:
    """The saved PPO directory check: c4_ppo_check.csv, else any *ppo_check*.csv (alphabetical)."""
    exact = inputs / PPO_CHECK_NAME
    if exact.is_file():
        return exact
    others = sorted(inputs.glob("*ppo_check*.csv"))
    return others[0] if others else None


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
    return {"inputs_dir": str(folder.resolve()), "workbook": str(workbook) if workbook else None,
            "ppo_check_csv": str(ppo) if ppo else None, "live_directory_check": ppo is None and live,
            "live_directory_check_unavailable": "" if live else why_not}
