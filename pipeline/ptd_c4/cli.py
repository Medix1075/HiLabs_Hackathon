"""Command line entry point.

  python -m ptd_c4 run                                   # C2's PPO rate files -> out/rates.sqlite + out/c4/
  python -m ptd_c4 run --workbook "Rate Benchmarking Individual Weights_Hackathon.xlsx"
  python -m ptd_c4 run --files a.json.gz --codes all     # any rate file, every billing code
  python -m ptd_c4 run --directory data/directory.csv    # payer directory as network evidence
  python -m ptd_c4 directory --fhir-base https://.../fhir --out data/directory.csv   # build that directory
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ptd_c2.cli import write_csv, write_xlsx
from ptd_c2.config import load_config as load_c2_config
from ptd_c3.codes import load_expected_codes

from . import __version__
from .config import load_config
from .db import DEFAULT_DB, connect, drop_c4, finish_c4, init_c4
from .directory import crawl_plannet, load_directory
from .extract import build_pairs, fanout_counts, pick_rates, tag_table_rows
from .ingest import ingest_file
from .network import tag_references

DEFAULT_C2_FILES = Path("out/c2/c2_ppo_rate_files.csv")
DEFAULT_C3_SCORES = Path("out/c3/c3_file_scores.csv")
RATE_FIELDS = ["npi", "tin", "tin_type", "code_type", "code", "negotiated_type", "billing_class", "setting", "pos",
               "modifier", "rate", "expiration_date", "network_tag", "network_confidence", "candidates",
               "distinct_rates", "rate_min", "rate_max", "pick_rule", "file_id"]
TAG_FIELDS = ["file_id", "ref_id", "network_name", "n_groups", "source", "tag", "tag_confidence", "ppo_evidence",
              "non_ppo_evidence", "evidence"]


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _targets(args, cfg, log) -> tuple[list[dict], str] | None:
    if args.files:
        return [{"rate_file_url": f, "from_c2": False} for f in args.files], "--files"
    c2_path = Path(args.c2 or DEFAULT_C2_FILES)
    if not c2_path.exists():
        print(f"C2 file list {c2_path} not found. Run C2 first (python -m ptd_c2 run) or pass --files.",
              file=sys.stderr)
        return None
    targets = [{**r, "from_c2": True} for r in _read_csv(c2_path) if r.get("rate_file_url")]
    c3_path = Path(args.c3 or DEFAULT_C3_SCORES)
    if c3_path.exists():
        scores = {r["rate_file_url"]: r for r in _read_csv(c3_path) if r.get("bucket") == "ALL"}
        min_score = cfg["outputs"].get("min_c3_score")
        for t in targets:
            s = scores.get(t["rate_file_url"], {})
            t["c3_score"], t["c3_grade"] = s.get("score"), s.get("grade")
        if min_score is not None:
            keep = [t for t in targets if t["c3_score"] in (None, "") or float(t["c3_score"]) >= float(min_score)]
            for t in targets:
                if t not in keep:
                    log(f"[input] skipped (C3 score {t['c3_score']} < {min_score}): {t['rate_file_url']}")
            targets = keep
    return targets, str(c2_path)


def run(args) -> int:
    cfg = load_config(args.config, args.as_of)
    c2cfg = load_c2_config(args.c2_config)
    if args.workers is not None:
        cfg["stream"]["workers"] = args.workers
    if args.codes:
        cfg["codes"]["scope"] = args.codes
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    started = datetime.now(timezone.utc)
    log(f"C4 v{__version__} started {started.isoformat()} config={cfg['_path']} as_of={cfg['_as_of']}")
    got = _targets(args, cfg, log)
    if got is None:
        return 1
    targets, source = got
    log(f"[input] {len(targets)} rate file(s) from {source}")

    codes = None
    if cfg["codes"]["scope"] == "workbook":
        if args.workbook:
            exp = load_expected_codes(args.workbook)
            codes = sorted(exp.codes)
            log(f"[input] code scope: {len(codes)} codes from {exp.source} (every item is still read for network evidence)")
        else:
            log("[input] code scope 'workbook' but no --workbook given: extracting every billing code")
    else:
        log("[input] code scope: every billing code")

    directory = None
    if args.directory:
        directory = load_directory(args.directory, c2cfg, cfg["network"]["ppo_labels"])
        log(f"[input] directory: {len(directory.rows):,} rows, {len(directory.by_npi):,} NPIs with a network side")

    db_path = Path(args.db)
    con = connect(db_path)
    drop_c4(con)
    init_c4(con)
    as_of = cfg["_as_of"].isoformat()
    summary_files, network_reports, extract_reports = [], {}, {}
    status = 0
    for file_id, t in enumerate(targets, start=1):
        url = t["rate_file_url"]
        log(f"[c4] file {file_id}: {url}")
        meta = {"from_c2": t.get("from_c2"), "needs_network_filter": str(t.get("needs_network_filter")).lower() == "true",
                "c2_network_label": t.get("network_label", ""), "c2_ppo_network_names": t.get("ppo_network_names", "")}
        row = {"file_id": file_id, "url": url, **meta, "c3_score": t.get("c3_score"), "c3_grade": t.get("c3_grade")}
        try:
            info = ingest_file(con, db_path, file_id, url, cfg, codes, log, args.max_items)
            row.update(local_path=info["local_path"], status="ok", items=info["items"],
                       items_in_scope=info["items_in_scope"], price_rows=info["price_rows"], seconds=info["seconds"],
                       all_price_rows=info["all_price_rows"], distinct_codes=info["distinct_codes"],
                       workbook_codes_found=info["workbook_codes_found"], code_types=info["code_types"],
                       references=info["refs"], size_bytes=os.path.getsize(info["local_path"]),
                       **{k: info["top"].get(k) for k in ("last_updated_on", "version", "reporting_entity_name")})
            log(f"[c4]   {info['items']:,} in_network items ({info['items_in_scope']:,} in code scope), "
                f"{info['price_rows']:,} price rows, {info['refs']:,} references in {info['seconds']}s")
            log(f"[c4]   whole file: {row['size_bytes']:,} bytes, {info['all_price_rows']:,} price rows over "
                f"{info['distinct_codes']:,} distinct codes (items by code type: {info['code_types']}); "
                + (f"{info['workbook_codes_found']:,} of the {len(codes):,} workbook codes are in the file"
                   if codes is not None else "no code scope"))
            t1 = time.time()
            network_reports[url] = tag_references(con, file_id, meta, cfg, c2cfg, directory, log)
            t2 = time.time()
            pairs = build_pairs(con, file_id, cfg["network"]["include_tags"], log)
            picked = pick_rates(con, file_id, cfg, as_of, log)
            t3 = time.time()
            row["phase_seconds"] = {"read": info["seconds"], "network": round(t2 - t1, 1), "extract": round(t3 - t2, 1)}
            log(f"[c4]   time: read {info['seconds']}s, network tagging {t2 - t1:.1f}s, extraction {t3 - t2:.1f}s")
            extract_reports[url] = {**pairs, **picked, **fanout_counts(con, file_id)}
            log(f"[c4]   {extract_reports[url]['rate_rows']:,} NPI x TIN x code x variant rates, "
                f"{extract_reports[url]['npis']:,} NPIs, {extract_reports[url]['tins']:,} TINs")
        except Exception as exc:  # one bad file must not stop the others
            status = 2
            row.update(status="error", error=f"{exc.__class__.__name__}: {exc}"[:500])
            log(f"[c4]   ERROR {row['error']}")
        con.execute("INSERT INTO c4_files (file_id, url, local_path, last_updated_on, version, reporting_entity_name, "
                    "needs_network_filter, c2_network_label, c2_ppo_network_names, c3_score, c3_grade, status, error, "
                    "items, items_in_scope, price_rows, seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row.get(k) for k in ("file_id", "url", "local_path", "last_updated_on", "version",
                                               "reporting_entity_name", "needs_network_filter", "c2_network_label",
                                               "c2_ppo_network_names", "c3_score", "c3_grade", "status", "error",
                                               "items", "items_in_scope", "price_rows", "seconds")))
        summary_files.append(row)
    finish_c4(con)

    # ---------------------------------------------------------------- outputs
    tags = [dict(zip(TAG_FIELDS, r)) for r in con.execute(f"SELECT {', '.join(TAG_FIELDS)} FROM c4_refs")]
    write_csv(out / "c4_network_tags.csv", tags, TAG_FIELDS)
    provider_tags = [r for fid in range(1, len(targets) + 1) for r in tag_table_rows(con, fid)]
    write_csv(out / "c4_provider_tags.csv", provider_tags, ["npi", "tin", "tag"])
    sample = []
    q = f"SELECT {', '.join(RATE_FIELDS)} FROM c4_rates"
    if cfg["outputs"]["rates_csv"]:
        with gzip.open(out / "c4_rates.csv.gz", "wt", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(RATE_FIELDS)
            for r in con.execute(q):
                w.writerow(r)
                if len(sample) < cfg["outputs"]["sample_rows_in_xlsx"]:
                    sample.append(dict(zip(RATE_FIELDS, r)))
    else:
        sample = [dict(zip(RATE_FIELDS, r)) for r in con.execute(q + f" LIMIT {cfg['outputs']['sample_rows_in_xlsx']}")]
    tag_summary = [{"rate_file_url": u, "tag": k, "references": v} for u, rep in network_reports.items()
                   for k, v in rep["tags"].items()]
    hyp = [{"rate_file_url": u, "hypothesis": h, **{k: v for k, v in rep[h].items()}}
           for u, rep in network_reports.items() for h in ("dual_contract", "propagation")]
    picks = [{"rate_file_url": u, "kind": kind, "key": k, "count": v} for u, rep in extract_reports.items()
             for kind in ("pick_rules", "dropped") for k, v in rep[kind].items()]
    write_csv(out / "c4_extraction_log.csv", picks, ["rate_file_url", "kind", "key", "count"])
    write_csv(out / "c4_hypotheses.csv", hyp)
    write_xlsx(out / "c4_results.xlsx", {
        "files": (summary_files, None), "network_tags": (tag_summary, None), "hypotheses": (hyp, None),
        "extraction_log": (picks, None), "rates_sample": (sample, RATE_FIELDS),
    })
    summary = {
        "version": __version__, "started": started.isoformat(), "finished": datetime.now(timezone.utc).isoformat(),
        "config": cfg["_path"], "as_of": as_of, "db": str(db_path), "source": source,
        "code_scope": len(codes) if codes is not None else "all", "directory": bool(directory),
        "files": summary_files, "network": network_reports, "extraction": extract_reports,
        "totals": {k: sum(r.get(k, 0) for r in extract_reports.values()) for k in ("rate_rows", "pairs", "signatures")},
        "distinct_npis": con.execute("SELECT COUNT(DISTINCT npi) FROM c4_pairs WHERE sig_id IN "
                                     "(SELECT sig_id FROM c4_sig_rates)").fetchone()[0],
        "distinct_tins": con.execute("SELECT COUNT(DISTINCT tin) FROM c4_pairs WHERE sig_id IN "
                                     "(SELECT sig_id FROM c4_sig_rates)").fetchone()[0],
    }
    (out / "c4_run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "c4_run_log.txt").write_text("\n".join(log_lines) + "\n")
    con.close()
    log(f"[done] rate store {db_path}; outputs in {out}")
    return status


def directory_cmd(args) -> int:
    c2cfg = load_c2_config(args.c2_config)
    n = crawl_plannet(args.fhir_base, args.out, c2cfg, args.network_filter, args.state, args.max_pages)
    print(f"[dir] wrote {n:,} rows to {args.out}")
    return 0 if n else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ptd_c4", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="tag networks and extract rates")
    r.add_argument("--c2", help=f"C2 rate-file list (default {DEFAULT_C2_FILES})")
    r.add_argument("--c3", help=f"C3 scores, carried onto each file (default {DEFAULT_C3_SCORES})")
    r.add_argument("--files", nargs="+", help="extract from these rate files (paths or URLs) instead of C2's list")
    r.add_argument("--workbook", help="HiLabs Rate Benchmarking Individual Weights workbook (code scope)")
    r.add_argument("--codes", choices=["workbook", "all"], help="code scope (default from config)")
    r.add_argument("--directory", help="directory table (CSV/XLSX: npi, tin, network) used as network evidence")
    r.add_argument("--db", default=str(DEFAULT_DB), help=f"rate store (default {DEFAULT_DB})")
    r.add_argument("--workers", type=int, help="in_network worker processes (default: CPU cores - 1)")
    r.add_argument("--max-items", type=int, help="read only the first N in_network items (a quick trial run)")
    r.add_argument("--config", help="config YAML (default config/c4.yaml)")
    r.add_argument("--c2-config", help="C2 config with the network-name rules (default config/c2.yaml)")
    r.add_argument("--as-of", help="judge expiry against this date (YYYY-MM-DD); default today")
    r.add_argument("--out", default="out/c4", help="output directory")
    r.set_defaults(func=run)
    d = sub.add_parser("directory", help="build a directory table from a FHIR Plan-Net provider directory")
    d.add_argument("--fhir-base", required=True, help="FHIR base URL, e.g. Centene's provider directory")
    d.add_argument("--out", default="data/directory.csv")
    d.add_argument("--network-filter", help="regex on network names to crawl (e.g. 'Health Net')")
    d.add_argument("--state", default="CA", help="keep locations in this state (empty = all)")
    d.add_argument("--max-pages", type=int, default=1000)
    d.add_argument("--c2-config", help="C2 config with the network-name rules")
    d.set_defaults(func=directory_cmd)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
