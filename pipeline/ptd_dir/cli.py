"""Build the payer directory table that confirms PPO providers (C4 --directory).

Runs on your own machine: the directory sites are JavaScript apps and are not reachable from every
network (the Cigna-hosted copy works outside California).

  # 1. search the directory by hand, with the PPO network chosen; every JSON result is recorded
  python -m ptd_dir capture --url "https://hcpdirectory.cigna.com/web/public/consumer/directory/search?consumerCode=HDC064" \\
      --network "Health Net PPO" --out data/directory/capture_ppo.jsonl

  # 2. (optional) repeat that search for every ZIP where C4's providers practise, page by page
  python -m ptd_dir targets --db out/rates.sqlite --what zip --out data/directory/zips.txt
  python -m ptd_dir replay --capture data/directory/capture_ppo.jsonl --vary zip=@data/directory/zips.txt \\
      --vary page=1..30 --out data/directory/replay_ppo.jsonl

  # 3. turn the captures into the directory table (NPIs found by name + address when not shown)
  python -m ptd_dir build --capture data/directory/*.jsonl --db out/rates.sqlite --out data/directory.csv

  # 4. use it
  python -m ptd_run all --from c4 --directory data/directory.csv --workbook ...

Capture the PPO search and, if you can, the HMO searches too (with --network naming them): a provider
listed only in HMO networks is evidence the other way.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from collections import Counter
from pathlib import Path

from .parse import ROW_FIELDS, parse_captures


def _paths(patterns: list[str]) -> list[Path]:
    out = []
    for p in patterns:
        hits = sorted(glob.glob(p))
        out += [Path(h) for h in hits] if hits else [Path(p)]
    return [p for p in out if p.exists() and not p.name.endswith(".cookies.json")]


def capture_cmd(args) -> int:
    from .capture import capture

    capture(args.url, Path(args.out), args.network, channel=args.channel, executable=args.browser,
            headless=args.headless, max_minutes=args.max_minutes, stop_after_seconds=args.stop_after)
    return 0


def replay_cmd(args) -> int:
    from .replay import replay

    st = replay(_paths(args.capture), Path(args.out), args.vary or [], index=args.index, delay=args.delay,
                max_requests=args.max_requests)
    return 0 if st["requests"] else 1


def targets_cmd(args) -> int:
    from .match import search_targets

    vals = search_targets(args.db, args.what)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(vals) + "\n")
    print(f"[dir] {len(vals):,} {args.what} values -> {args.out}")
    return 0


def build_cmd(args) -> int:
    from .match import assign_npis

    paths = _paths(args.capture)
    if not paths:
        print("no capture files found", file=sys.stderr)
        return 1
    rows = parse_captures(paths, args.network)
    print(f"[dir] {len(rows):,} provider x location listings in {len(paths)} capture file(s)")
    st = assign_npis(rows, args.db if args.db and Path(args.db).exists() else None, use_api=args.npi_api)
    print("[dir] NPIs: " + ", ".join(f"{k} {v:,}" for k, v in sorted(st.items())))
    keep = [r for r in rows if r.get("npi") or args.keep_unmatched]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ROW_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(keep)
    nets = Counter(r["network_name"] for r in keep)
    report = {"listings": len(rows), "written": len(keep), "npi_sources": st, "by_network_name": dict(nets),
              "captures": [str(p) for p in paths]}
    Path(args.out).with_suffix(".summary.json").write_text(json.dumps(report, indent=2))
    print(f"[dir] wrote {len(keep):,} rows with an NPI to {args.out}; networks: {dict(nets)}")
    if any(not n for n in nets):
        print("[dir] WARNING: some rows have no network_name. Pass --network to say which network was searched; "
              "C4 treats rows without a named network as 'listed with the payer', a candidate only.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ptd_dir", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="open the directory in a browser and record its search results")
    c.add_argument("--url", required=True)
    c.add_argument("--network", required=True, help="the network you will select, e.g. 'Health Net PPO'")
    c.add_argument("--out", default="data/directory/capture.jsonl")
    c.add_argument("--channel", help="chrome or msedge (default: try both, then Playwright's Chromium)")
    c.add_argument("--browser", help="path to a Chromium-based browser executable")
    c.add_argument("--headless", action="store_true")
    c.add_argument("--max-minutes", type=float, default=180)
    c.add_argument("--stop-after", type=float, help=argparse.SUPPRESS)  # tests: stop after N seconds
    c.set_defaults(func=capture_cmd)
    r = sub.add_parser("replay", help="re-send a captured search with other values")
    r.add_argument("--capture", nargs="+", required=True)
    r.add_argument("--vary", action="append", help="name=v1,v2 | name=@file | name=1..30 (repeatable)")
    r.add_argument("--index", type=int, help="which captured response to replay (default: the one with most providers)")
    r.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    r.add_argument("--max-requests", type=int, default=5000)
    r.add_argument("--out", default="data/directory/replay.jsonl")
    r.set_defaults(func=replay_cmd)
    t = sub.add_parser("targets", help="ZIPs / NPIs / names of C4's providers, to vary in a replay")
    t.add_argument("--db", default="out/rates.sqlite")
    t.add_argument("--what", choices=["zip", "npi", "name"], default="zip")
    t.add_argument("--out", default="data/directory/targets.txt")
    t.set_defaults(func=targets_cmd)
    b = sub.add_parser("build", help="captures -> directory table for C4/C5 (--directory)")
    b.add_argument("--capture", nargs="+", required=True, help="capture / replay JSONL files (globs allowed)")
    b.add_argument("--network", help="override the network name recorded at capture time")
    b.add_argument("--db", default="out/rates.sqlite", help="rate store from an earlier run (to find NPIs by name)")
    b.add_argument("--npi-api", action="store_true", help="also search the NPI Registry by name + ZIP")
    b.add_argument("--keep-unmatched", action="store_true", help="also write listings that got no NPI")
    b.add_argument("--out", default="data/directory.csv")
    b.set_defaults(func=build_cmd)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
