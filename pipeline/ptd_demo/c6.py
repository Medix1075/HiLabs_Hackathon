"""Temporary C6 demo wrapper.

C6 still uses the actual C4 rates and C5 address rows. Use the reviewed CSV only
as the small roster for C4/C5; no rate is invented from the roster.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from pathlib import Path

from ptd_c4.db import connect
from ptd_c6 import cli as c6
from .roster import DEFAULT_DEMO_CSV, load_demo_rows


def run(args):
    # Guard the demo so it cannot accidentally read the production/default database.
    rows = load_demo_rows(args.demo_csv)
    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"Demo rate store not found: {db}. Run python -m ptd_demo.c4 first.")
    c6.main(["run", "--db", str(db), "--workbook", args.workbook, "--out", args.out] +
            (["--nucc", args.nucc] if args.nucc else []) +
            (["--config", args.config] if args.config else []))
    summary_path = Path(args.out) / "c6_run_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary["demo_mode"] = True
    summary["demo_roster"] = str(args.demo_csv)
    summary["demo_roster_rows"] = len(rows)
    summary["demo_unique_pairs"] = len({(r["npi"], r["rate_file_tin"]) for r in rows if r["rate_file_tin"]})
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"[done] C6 demo outputs: {args.out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo-csv", default=str(DEFAULT_DEMO_CSV))
    ap.add_argument("--db", default="out/demo/rates.sqlite")
    ap.add_argument("--workbook", required=True, help="HiLabs Rate Benchmarking Individual Weights workbook")
    ap.add_argument("--nucc")
    ap.add_argument("--config")
    ap.add_argument("--out", default="out/demo/c6")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
