"""Step 7: spot-check against outside sources (semi-automated; no directory scraping).

`make_sample` writes a sheet of randomly chosen classified plans with blank source columns.
A person fills in what Health Net's Summary of Benefits / provider directory says, then
`score_sample` computes the match column and the overall agreement rate.
"""
from __future__ import annotations

import csv
import random
import re
from pathlib import Path
from urllib.parse import quote_plus

SAMPLE_FIELDS = ["row_id", "plan_name", "plan_id", "rate_file_url", "our_label", "our_ppo_variant",
                 "our_confidence", "source_label", "source_url", "source_notes", "search_hint"]


def make_sample(rows: list[dict], cfg: dict) -> list[dict]:
    vc = cfg["validation"]
    pool = [r for r in rows if r["in_scope"]]
    rng = random.Random(vc["seed"])
    sample = rng.sample(pool, min(vc["sample_size"], len(pool)))
    return [{
        "row_id": r["row_id"], "plan_name": r["plan_name"], "plan_id": r["plan_id"],
        "rate_file_url": r["rate_file_url"], "our_label": r["final_network_type"],
        "our_ppo_variant": r["ppo_variant"], "our_confidence": r["confidence"],
        "source_label": "", "source_url": "", "source_notes": "",
        "search_hint": "https://www.google.com/search?q=" + quote_plus(f'"Health Net" "{r["plan_name"]}" summary of benefits'),
    } for r in sorted(sample, key=lambda r: r["row_id"])]


def _canon(label: str) -> str:
    s = (label or "").upper()
    for t in ("PPO", "HMO", "EPO", "POS", "HSP"):
        if re.search(rf"\b{t}\b", s):
            return t
    return s.strip()


def score_sample(path: str | Path) -> tuple[list[dict], dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    checked = 0
    agree = 0
    for r in rows:
        if not (r.get("source_label") or "").strip():
            r["match"] = ""
            continue
        checked += 1
        ok = _canon(r["our_label"]) == _canon(r["source_label"])
        r["match"] = "yes" if ok else "no"
        agree += ok
    summary = {"sample_rows": len(rows), "checked": checked, "agree": agree,
               "agreement_rate": round(agree / checked, 4) if checked else None}
    return rows, summary
