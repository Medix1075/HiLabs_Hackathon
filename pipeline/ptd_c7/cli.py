"""Command line entry point.

  python -m ptd_c7 run --workbook "Rate Benchmarking Individual Weights_Hackathon.xlsx"   # -> out/c7/
  python -m ptd_c7 run --set candidates            # score the candidate (unconfirmed) rates on their own

Reads C6's rate table from the rate store, benchmarks every rate against similar providers, scores
every provider (NPI + TIN) and TIN, and writes deliverable 3 plus the rate file with cost columns.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ptd_c2.cli import write_csv, write_xlsx
from ptd_c3.codes import load_expected_codes
from ptd_c4.db import DEFAULT_DB, connect, table_exists
from ptd_c6.cli import COLUMNS as C6_COLUMNS
from ptd_c6.cli import ExcelWriter
from ptd_c6.specialty import load_specialties

from . import __version__
from .bench import benchmark
from .report import write_metrics
from .score import provider_scores, tin_scores

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "c7.yaml"
BENCH_FIELDS = ["npi", "tin", "npi_type", "specialty", "spec_cd", "region", "code_type", "code", "negotiated_type",
                "billing_class", "setting", "pos", "modifier", "rate", "peer_level", "peer_n", "peer_q1", "peer_median",
                "peer_q3", "rate_index", "percentile", "quartile", "network_status", "network_tag"]
PROVIDER_FIELDS = ["npi", "tin", "npi_type", "specialty", "spec_cd", "region", "cost_score", "cost_index", "tier",
                   "score_confidence", "raw_cost_index", "n_eff", "codes_benchmarked", "share_cheapest_quartile",
                   "share_most_expensive_quartile", "specialty_code_coverage", "cohort", "cohort_size",
                   "rank_in_cohort", "peer_levels_used"]
EXTRA_COLUMNS = [("Peer Median", "peer_median"), ("Rate vs Peer Median", "rate_index"),
                 ("Peer Percentile", "percentile"), ("Peer Group", "peer_level"), ("Peers", "peer_n"),
                 ("Provider Cost Index", "cost_index"), ("Provider Cost Score", "cost_score"),
                 ("Provider Tier", "tier"), ("Score Confidence", "score_confidence")]


def load_config(path=None) -> dict:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_path"] = str(path or DEFAULT_CONFIG)
    return cfg


def run(args) -> int:
    cfg = load_config(args.config)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    started = datetime.now(timezone.utc)
    log(f"C7 v{__version__} started {started.isoformat()} config={cfg['_path']}")
    con = connect(args.db)
    if not table_exists(con, "c6_rates"):
        print(f"No C6 rate table in {args.db}. Run C6 first (python -m ptd_c6 run).", file=sys.stderr)
        return 1
    sp = load_specialties(args.workbook) if args.workbook else None
    overall = load_expected_codes(args.workbook).weights if args.workbook else {}
    if not args.workbook:
        cfg["provider_score"]["weighting"] = "uniform"
        log("[input] no --workbook: every code weighs the same in the provider score")

    cols = [d[1] for d in con.execute("PRAGMA table_info(c6_rates)")]
    rows, excluded = [], Counter()
    excl = {t.lower() for t in cfg["exclude_negotiated_types"]}
    tags = {t.upper() for t in args.network_tags} if args.network_tags else None
    statuses = set(args.network_statuses or cfg["sets"][args.set]["network_statuses"])
    log(f"[input] set '{args.set}': rates with network status {', '.join(sorted(statuses))}; peers come from the "
        "same set only, so unconfirmed rates never move the confirmed benchmark")
    for rec in con.execute("SELECT * FROM c6_rates"):
        r = dict(zip(cols, rec))
        if r.get("network_status") not in statuses:
            excluded[f"network_status {r.get('network_status')}"] += 1
            continue
        if r["negotiated_type"] in excl:
            excluded[f"negotiated_type {r['negotiated_type']}"] += 1
            continue
        if tags and str(r["network_tag"]).upper() not in tags:
            excluded[f"network_tag {r['network_tag']}"] += 1
            continue
        if not r["rate"] or r["rate"] <= 0:
            excluded["no rate"] += 1
            continue
        r["specialty"] = r["spec_cd"] or r["specialty"]  # peer key: the HiLabs code when there is one
        r["specialty_name"] = r.get("specialty")
        r["region"] = r["region"] or "(no address)"
        r["npi_type"] = r["npi_type"] or "?"
        rows.append(r)
    log(f"[input] {len(rows):,} provider x code rates to benchmark; left out: "
        + (", ".join(f"{k} {v:,}" for k, v in excluded.items()) or "none"))
    names = {}
    for rec in con.execute("SELECT DISTINCT spec_cd, specialty FROM c6_rates"):
        names[rec[0] or rec[1]] = rec[1]

    benchmark(rows, cfg)
    providers = provider_scores(rows, cfg, sp, overall)
    for p in providers:
        p["specialty"] = names.get(p["specialty"], p["specialty"])
    for r in rows:
        r["specialty"] = names.get(r["specialty"], r["specialty"])
    tins = tin_scores(providers, cfg)
    log(f"[c7] {len(providers):,} providers and {len(tins):,} TINs scored; tiers: "
        + ", ".join(f"{k} {v:,}" for k, v in Counter(p["tier"] for p in providers).most_common()))

    table = "c7_provider_scores" + ("" if args.set == "confirmed" else f"_{args.set}")
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(f"CREATE TABLE {table} ({', '.join(PROVIDER_FIELDS)})")
    con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(PROVIDER_FIELDS))})",
                    [tuple(p.get(k) for k in PROVIDER_FIELDS) for p in providers])
    con.commit()
    with gzip.open(out / "c7_rate_benchmarks.csv.gz", "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=BENCH_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    write_csv(out / "c7_provider_scores.csv", providers, PROVIDER_FIELDS)
    write_csv(out / "c7_tin_scores.csv", tins)
    peer_groups = Counter((r["peer_level"], r["code"]) for r in rows)
    levels = [{"peer_level": k, "rates": v} for k, v in Counter(r["peer_level"] for r in rows).most_common()]
    write_xlsx(out / "c7_cost_effectiveness.xlsx", {
        "providers": (sorted(providers, key=lambda p: (-p["cost_score"], p["npi"]))[:500000], PROVIDER_FIELDS),
        "tins": (tins[:500000], None), "peer_levels": (levels, None),
        "codes_by_level": ([{"peer_level": k[0], "code": k[1], "rates": v} for k, v in peer_groups.most_common(5000)], None),
    })
    write_metrics(out / "c7_metrics.md", cfg, rows, providers, tins, excluded, bool(args.workbook))

    scored_rows = 0
    c6_csv = Path(args.c6_out) / f"{cfg['sets'][args.set]['c6_file']}.csv.gz"
    if cfg["outputs"]["scored_rate_file"] and c6_csv.exists():
        from ptd_c6.cli import load_config as load_c6_config

        scored_rows = _scored_rate_file(c6_csv, out, rows, providers, load_c6_config()["excel"])
        log(f"[c7] rate file with cost columns: {scored_rows:,} rows")
    summary = {"version": __version__, "started": started.isoformat(), "finished": datetime.now(timezone.utc).isoformat(),
               "set": args.set, "network_statuses": sorted(statuses),
               "rates_benchmarked": sum(1 for r in rows if r.get("peer_ok")), "rates_without_peers":
               sum(1 for r in rows if not r.get("peer_ok")), "excluded": dict(excluded), "providers_scored": len(providers),
               "tins_scored": len(tins), "tiers": dict(Counter(p["tier"] for p in providers)),
               "score_confidence": dict(Counter(p["score_confidence"] for p in providers)),
               "peer_levels": {lv["peer_level"]: lv["rates"] for lv in levels}, "scored_rate_file_rows": scored_rows}
    (out / "c7_run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "c7_run_log.txt").write_text("\n".join(log_lines) + "\n")
    con.close()
    log(f"[done] outputs in {out} (metrics file: {out / 'c7_metrics.md'})")
    return 0


def _scored_rate_file(c6_csv: Path, out: Path, rows: list[dict], providers: list[dict], excel_cfg: dict) -> int:
    """C6's rate file with the peer benchmark and the provider score appended to every row."""
    bench = {(str(r["npi"]), r["tin"], r["code_type"], r["code"]): r for r in rows}
    prov = {(str(p["npi"]), p["tin"]): p for p in providers}
    header = [c for c, _ in C6_COLUMNS] + [c for c, _ in EXTRA_COLUMNS]
    xl = ExcelWriter(out / "c7_provider_rate_file_scored.xlsx", excel_cfg, header)
    n = 0
    idx = {c: i for i, (c, _) in enumerate(C6_COLUMNS)}
    with gzip.open(c6_csv, "rt", newline="", encoding="utf-8") as fin, \
            gzip.open(out / "c7_provider_rate_file_scored.csv.gz", "wt", newline="", encoding="utf-8") as fout:
        rd, wr = csv.reader(fin), csv.writer(fout)
        next(rd)
        wr.writerow(header)
        for row in rd:
            key = (row[idx["NPI"]], row[idx["TIN"]], row[idx["Billing Code Type"]], row[idx["Billing Code"]])
            b = bench.get(key, {})
            p = prov.get(key[:2], {})
            extra = [b.get("peer_median", ""), b.get("rate_index", ""), b.get("percentile", ""),
                     b.get("peer_level", ""), b.get("peer_n", ""), p.get("cost_index", ""), p.get("cost_score", ""),
                     p.get("tier", ""), p.get("score_confidence", "")]
            wr.writerow(row + extra)
            xl.add(row + extra)
            n += 1
    xl.close([["Provider rate file with cost-effectiveness columns (deliverables 1 + 3)", ""],
              ["Rows", n], ["Cost columns", "see c7_metrics.md"]])
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ptd_c7", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="benchmark rates and score providers")
    r.add_argument("--db", default=str(DEFAULT_DB))
    r.add_argument("--workbook", help="HiLabs Rate Benchmarking Individual Weights workbook (code weights)")
    r.add_argument("--set", default="confirmed", choices=["confirmed", "candidates"],
                   help="which rates to score (config/c7.yaml `sets`): confirmed PPO (default) or the candidates")
    r.add_argument("--network-statuses", nargs="+", help="override the set's C4 network statuses")
    r.add_argument("--network-tags", nargs="+", help="also require these C4 network tags (e.g. PPO)")
    r.add_argument("--config", help="config YAML (default config/c7.yaml)")
    r.add_argument("--c6-out", default="out/c6", help="C6 output folder (for the scored rate file)")
    r.add_argument("--out", default="out/c7")
    r.set_defaults(func=run)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
