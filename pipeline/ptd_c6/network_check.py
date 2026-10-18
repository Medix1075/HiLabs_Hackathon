"""How far to trust C4's network status: coverage now, precision once a sample is checked by hand.

Coverage needs no outside data: how many provider references, NPI-TIN pairs and rate rows sit in each
status. Precision needs ground truth, which only the payer's own directory gives, and the directory
can't be read in bulk from everywhere. So C6 writes a stratified sample (c6_network_check_sample.csv):
up to N pairs per status, with the name and address needed to look each one up. Someone searches
each provider in the Health Net PPO directory and fills in `directory_result`:

    PPO          listed in the target PPO network (at this TIN/address or not: say so in notes)
    NOT_PPO      listed, but only in other networks (HMO, EOA, ...)
    NOT_LISTED   not found in the directory at all

`python -m ptd_c6 check-network --sample <filled csv>` then reports, per status, the share found
in the PPO network with a 95% Wilson interval: precision for CONFIRMED_PPO, and for PPO_CANDIDATE /
UNRESOLVED how many PPO providers the confirmation rule still misses. The checked sample is used only
to measure, never fed back as evidence, so the measurement stays honest.
"""
from __future__ import annotations

import csv
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from ptd_c4.db import table_exists

SAMPLE_FIELDS = ["network_status", "npi", "tin", "name", "specialty", "address", "city", "zip", "status_basis",
                 "directory_result", "directory_network_shown", "checked_on", "checked_by", "notes"]
RESULTS = {"PPO", "NOT_PPO", "NOT_LISTED"}
ORDER = ["CONFIRMED_PPO", "PPO_CANDIDATE", "UNRESOLVED"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def coverage(con) -> dict:
    out = {"references": dict(con.execute("SELECT status, COUNT(*) FROM c4_refs GROUP BY 1").fetchall())}
    out["npi_tin_pairs"] = dict(con.execute("SELECT network_status, COUNT(*) FROM (SELECT DISTINCT npi, tin, "
                                            "network_status FROM c6_rates) GROUP BY 1").fetchall())
    out["npis"] = dict(con.execute("SELECT network_status, COUNT(DISTINCT npi) FROM c6_rates GROUP BY 1").fetchall())
    out["pair_codes"] = dict(con.execute("SELECT network_status, COUNT(*) FROM c6_rates GROUP BY 1").fetchall())
    if table_exists(con, "c4_oon_providers"):
        out["oon_npis"] = con.execute("SELECT COUNT(DISTINCT npi) FROM c4_oon_providers").fetchone()[0]
    return out


def make_sample(con, per_status: int, seed: int = 7) -> list[dict]:
    """Up to per_status NPI-TIN pairs for each status in the rate table, with what is needed to look them up."""
    pairs = defaultdict(list)
    for npi, tin, st, spec in con.execute("SELECT DISTINCT npi, tin, network_status, specialty FROM c6_rates "
                                          "ORDER BY npi, tin"):
        pairs[st].append((npi, tin, spec))
    names = dict(con.execute("SELECT npi, name FROM c5_providers").fetchall()) if table_exists(con, "c5_providers") else {}
    addr = {}
    if table_exists(con, "c5_addresses"):
        for npi, tin, a, city, z in con.execute("SELECT npi, tin, address, city, zip5 FROM c5_addresses WHERE rank = 1"):
            addr[(npi, tin)] = (a, city, z)
    basis = {}
    if "status_basis" in {r[1] for r in con.execute("PRAGMA table_info(c4_pairs)")}:
        q = "SELECT npi, tin, MIN(status_basis) FROM c4_pairs GROUP BY 1, 2"
    else:  # older store: the basis of the pair's references
        q = ("SELECT p.npi, p.tin, MIN(r.status_basis) FROM c4_pairs p JOIN c4_sig_members m ON m.file_id = p.file_id "
             "AND m.sig_id = p.sig_id JOIN c4_refs r ON r.file_id = m.file_id AND r.ref_id = m.ref_id GROUP BY 1, 2")
    for npi, tin, b in con.execute(q):
        basis[(npi, tin)] = b
    rnd = random.Random(seed)
    rows = []
    for st in ORDER + sorted(set(pairs) - set(ORDER)):
        got = pairs.get(st, [])
        for npi, tin, spec in sorted(rnd.sample(got, min(per_status, len(got)))):
            a, city, z = addr.get((npi, tin), ("", "", ""))
            rows.append({"network_status": st, "npi": npi, "tin": tin, "name": names.get(npi, ""), "specialty": spec,
                         "address": a, "city": city, "zip": z, "status_basis": basis.get((npi, tin), "")})
    return rows


def write_sample(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def score_sample(path: Path) -> dict:
    by = defaultdict(Counter)
    bad = 0
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            res = str(r.get("directory_result") or "").strip().upper().replace(" ", "_")
            if not res:
                continue
            if res not in RESULTS:
                bad += 1
                continue
            by[r["network_status"]][res] += 1
    out = {}
    for st, c in by.items():
        n, k = sum(c.values()), c["PPO"]
        lo, hi = wilson(k, n)
        out[st] = {"checked": n, "found_in_ppo": k, "not_ppo": c["NOT_PPO"], "not_listed": c["NOT_LISTED"],
                   "share_ppo": round(k / n, 4) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}
    return {"by_status": out, "unrecognised_results": bad}


def write_report(path: Path, cov: dict, checked: dict | None, sample_file: str) -> None:
    def table(d: dict) -> str:
        keys = ORDER + ["CONFIRMED_NON_PPO"] + sorted(set(d) - set(ORDER) - {"CONFIRMED_NON_PPO"})
        return " | ".join(f"{k}: {d.get(k, 0):,}" for k in keys if k in d or k in ORDER)

    lines = ["# Network status: coverage and accuracy", "",
             "C4 gives every provider reference one of four statuses (see the logic file). Deliverable 1 holds "
             "CONFIRMED_PPO rates only; PPO_CANDIDATE and UNRESOLVED rates are in the candidate file.", "",
             "## Coverage (this run)", "",
             f"- Provider references: {table(cov['references'])}",
             f"- NPI-TIN pairs with rates: {table(cov['npi_tin_pairs'])}",
             f"- NPIs with rates: {table(cov['npis'])}",
             f"- Pair x code rates: {table(cov['pair_codes'])}"]
    if "oon_npis" in cov:
        lines.append(f"- NPIs paid out of network in the allowed-amount files: {cov['oon_npis']:,}")
    lines += ["", "## Accuracy (hand-checked sample)", ""]
    if not checked or not checked["by_status"]:
        lines += [f"Not measured yet. Look up each provider in `{sample_file}` in Health Net's PPO directory, fill in "
                  "`directory_result` (PPO, NOT_PPO or NOT_LISTED) and run "
                  f"`python -m ptd_c6 check-network --sample {sample_file}`."]
    else:
        lines += ["| Status | Checked | In the PPO network | Not PPO | Not listed | Share PPO (95% CI) |",
                  "|---|---|---|---|---|---|"]
        for st in ORDER + sorted(set(checked["by_status"]) - set(ORDER)):
            r = checked["by_status"].get(st)
            if not r:
                continue
            lines.append(f"| {st} | {r['checked']} | {r['found_in_ppo']} | {r['not_ppo']} | {r['not_listed']} | "
                         f"{r['share_ppo']:.0%} ({r['ci95'][0]:.0%} to {r['ci95'][1]:.0%}) |")
        lines += ["", "For CONFIRMED_PPO the share is the precision of deliverable 1. For the other statuses it is "
                  "how many PPO providers the confirmation rule leaves out (the cost of being strict)."]
        if checked.get("unrecognised_results"):
            lines.append(f"\n{checked['unrecognised_results']} rows had a result other than PPO / NOT_PPO / "
                         "NOT_LISTED and were ignored.")
    path.write_text("\n".join(lines) + "\n")
    path.with_suffix(".json").write_text(json.dumps({"coverage": cov, "checked": checked}, indent=2, default=str))
