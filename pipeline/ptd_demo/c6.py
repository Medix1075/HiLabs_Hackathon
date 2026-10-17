"""Temporary C6 demo entry point over the isolated demo C4/C5 database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ptd_c6 import cli as c6
from .roster import DEFAULT_DEMO_CSV, load_demo_rows


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
    if not Path(args.db).exists():
        raise SystemExit(f"Demo rate store not found: {args.db}. Run python -m ptd_demo.c4 first.")
    cmd = ["run", "--db", args.db, "--workbook", args.workbook, "--out", args.out]
    if args.nucc: cmd += ["--nucc", args.nucc]
    if args.config: cmd += ["--config", args.config]
    c6.main(cmd)
    summary_path = Path(args.out) / "c6_run_summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    summary.update(demo_mode=True, demo_roster=args.demo_csv,
                   demo_roster_rows=len(rows),
                   demo_unique_pairs=len({(r["npi"], r["rate_file_tin"]) for r in rows if r["rate_file_tin"]}))
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"[done] C6 demo outputs: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
