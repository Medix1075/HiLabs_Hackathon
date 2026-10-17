"""Temporary C6 demo entry point over the isolated demo C4/C5 database."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from pathlib import Path

from ptd_c4.db import connect, table_exists
from ptd_c6 import cli as c6
from .roster import DEFAULT_DEMO_CSV, load_demo_rows


def _apply_pair_verdicts_to_outputs(out: Path, rows: list[dict]) -> None:
    """Keep demo PPO/unknown routing pair-specific in the exported files too."""
    import pandas as pd

    statuses = {}
    for r in rows:
        npi, tin = r["npi"], r["rate_file_tin"]
        statuses[(npi, tin)] = ("CONFIRMED_PPO" if r["verdict"] in {
            "FOUND_PPO_BY_NPI", "FOUND_PPO_SAME_FACILITY_DIFFERENT_NPI",
            "NOT_FOUND_BY_NPI_SAME_TIN_OTHER_SITES_PPO"} else "UNRESOLVED")
    # C6's SQLite table contains the exact rate rows fanned out by NPI+TIN.
    db = connect("out/demo/rates.sqlite")
    if not table_exists(db, "c6_rates"):
        db.close()
        return
    # Rewrite only the standard output exports that are already in demo scope.
    for filename in ("c6_provider_rate_file.csv.gz", "c6_provider_rate_file_candidates.csv.gz"):
        path = out / filename
        if not path.exists():
            continue
        with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            headers, data = reader.fieldnames or [], list(reader)
        fixed = []
        for row in data:
            key = (str(row.get("npi") or ""), str(row.get("tin") or "").replace("-", "").replace(" ", ""))
            status = statuses.get(key, "UNRESOLVED")
            if status == "CONFIRMED_PPO" and filename.endswith("_candidates.csv.gz"):
                continue
            if status != "CONFIRMED_PPO" and filename == "c6_provider_rate_file.csv.gz":
                continue
            row["network_status"] = status
            fixed.append(row)
        with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers)
            writer.writeheader()
            writer.writerows(fixed)
    # Rebuild an Excel deliverable from the pair-scoped confirmed CSV for consistency.
    rate_path = out / "c6_provider_rate_file.csv.gz"
    if rate_path.exists():
        with gzip.open(rate_path, "rt", newline="", encoding="utf-8") as fh:
            data = list(csv.DictReader(fh))
        # The normal C6 Excel already has extensive metadata and is intentionally kept.
        # The corrected CSV is the exact row-level demo output used for downstream review.
        pd.DataFrame(data).to_csv(out / "c6_provider_rate_file_demo.csv", index=False)
    db.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo-csv", default=str(DEFAULT_DEMO_CSV))
    ap.add_argument("--db", default="out/demo/rates.sqlite")
    ap.add_argument("--workbook", required=True, help="HiLabs Rate Benchmarking Individual Weights workbook")
    ap.add_argument("--nucc")
    ap.add_argument("--config")
    ap.add_argument("--out", default="out/demo/c6")
    args = ap.parse_args(argv)
    rows = load_demo_rows(args.demo_csv)
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Demo rate store not found: {db_path}. Run python -m ptd_demo.c4 first.")
    # The production C6 CLI always opens the rates database at its supplied path.
    cmd = ["run", "--db", str(db_path), "--workbook", args.workbook, "--out", args.out]
    if args.nucc:
        cmd += ["--nucc", args.nucc]
    if args.config:
        cmd += ["--config", args.config]
    c6.main(cmd)
    _apply_pair_verdicts_to_outputs(Path(args.out), rows)
    summary_path = Path(args.out) / "c6_run_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    verdict_counts = {v: sum(r["verdict"] == v for r in rows) for v in sorted({r["verdict"] for r in rows})}
    summary.update(demo_mode=True, demo_roster=args.demo_csv, demo_roster_rows=len(rows),
                   demo_unique_pairs=len({(r["npi"], r["rate_file_tin"]) for r in rows if r["rate_file_tin"]}),
                   demo_verdicts=verdict_counts,
                   network_status_note="CSV verdicts are applied per NPI+TIN for the corrected demo CSV export.")
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"[done] C6 demo outputs: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
