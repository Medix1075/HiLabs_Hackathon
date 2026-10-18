"""Command line entry point.

  python -m ptd_c4 run                                   # C2's PPO rate files -> out/rates.sqlite + out/c4/
  python -m ptd_c4 run --workbook "Rate Benchmarking Individual Weights_Hackathon.xlsx"
  python -m ptd_c4 run --files a.json.gz --codes all     # any rate file, every billing code
  python -m ptd_c4 run --directory data/directory.csv    # payer directory as network evidence
  python -m ptd_c4 run --directory data/directory.csv --directory-checks "data/directory/*.coverage.jsonl" \
      --npis ppo_check_results.csv                        # only these NPIs; writes c4_ppo_check.csv
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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ptd_c2.cli import write_csv, write_xlsx
from ptd_c2.config import load_config as load_c2_config
from ptd_c3.codes import load_expected_codes

from . import __version__
from .config import load_config
from .db import DEFAULT_DB, connect, drop_c4, finish_c4, init_c4
from .directory import crawl_plannet, load_directory
from .oon import read_oon_providers, store_oon
from .extract import build_pairs, fanout_counts, pick_rates, tag_table_rows
from .ingest import ingest_file
from .network import STATUSES, tag_references
from . import dircheck, ppo_check

DEFAULT_C2_FILES = Path("out/c2/c2_ppo_rate_files.csv")
DEFAULT_C3_SCORES = Path("out/c3/c3_file_scores.csv")
RATE_FIELDS = ["npi", "tin", "tin_type", "code_type", "code", "negotiated_type", "billing_class", "setting", "pos",
               "modifier", "rate", "expiration_date", "network_status", "network_tag", "network_confidence", "candidates",
               "distinct_rates", "rate_min", "rate_max", "pick_rule", "file_id"]
TAG_FIELDS = ["file_id", "ref_id", "network_name", "n_groups", "source", "status", "status_basis", "tag",
              "tag_confidence", "ppo_evidence",
              "non_ppo_evidence", "evidence"]


def read_npi_list(values: list[str] | None) -> list[str]:
    """NPIs from the command line: NPIs themselves, text files of NPIs, or CSV files with an `npi` column."""
    out = []
    for v in values or []:
        p = Path(v)
        if p.is_file():
            text = p.read_text(encoding="utf-8-sig")
            first = text.splitlines()[0] if text else ""
            if "npi" in first.lower() and ("," in first or "\t" in first):
                rows = csv.DictReader(text.splitlines(), delimiter="," if "," in first else "\t")
                col = next(c for c in rows.fieldnames if c.strip().lower() == "npi")
                out += [str(r[col]).strip() for r in rows if str(r[col]).strip()]
            else:
                out += text.split()
        else:
            out += [x for x in str(v).replace(",", " ").split() if x]
    return list(dict.fromkeys(n.split(".")[0] for n in out if n.split(".")[0].isdigit()))


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _targets(args, cfg, log) -> tuple[list[dict], str] | None:
    if args.files:
        oon = [(u, bool(args.oon_ppo_only)) for u in args.oon or []]
        return [{"rate_file_url": f, "from_c2": False, "oon_files": oon} for f in args.files], "--files"
    c2_path = Path(args.c2 or DEFAULT_C2_FILES)
    if not c2_path.exists():
        print(f"C2 file list {c2_path} not found. Run C2 first (python -m ptd_c2 run) or pass --files.",
              file=sys.stderr)
        return None
    targets = [{**r, "from_c2": True} for r in _read_csv(c2_path) if r.get("rate_file_url")]
    aa_path = c2_path.parent / "c2_allowed_amount_files.csv"
    ppo_only = {r["allowed_amount_file_url"]: str(r.get("ppo_only")).lower() == "true"
                for r in (_read_csv(aa_path) if aa_path.exists() else [])}
    for t in targets:
        t["oon_files"] = [(u, ppo_only.get(u, False)) for u in str(t.get("allowed_amount_file_urls") or "").split("|") if u]
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


def _read_oon(con, file_id: int, oon_files: list, cfg: dict, log) -> list[dict]:
    """Load the providers of the file's allowed-amount files (network evidence, see oon.py)."""
    out = []
    if not cfg["network"].get("oon", {}).get("enabled", True):
        return out
    for url, ppo_only in oon_files:
        try:
            seen, st = read_oon_providers(url, cfg["stream"], log)
            store_oon(con, file_id, url, ppo_only, seen)
            log(f"[c4]   allowed-amount file {url}: {st['items']:,} out-of-network items, {st['providers']:,} "
                f"NPI-TIN pairs paid out of network ({'listed only by PPO plans' if ppo_only else 'shared with non-PPO plans'})")
            out.append({"url": url, "ppo_only": ppo_only, **st})
        except Exception as exc:  # optional evidence: keep going
            log(f"[c4]   allowed-amount file {url}: not read ({exc.__class__.__name__}: {exc})")
            out.append({"url": url, "ppo_only": ppo_only, "error": f"{exc.__class__.__name__}: {exc}"[:300]})
    return out


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

    npis = set(read_npi_list(args.npis)) if args.npis else None
    if npis is not None:
        log(f"[input] NPI limit: {len(npis):,} NPIs (network evidence still uses the whole file; pairs, rates and "
            "the steps after C4 cover only these)")
    directory = None
    if args.directory:
        directory = load_directory(args.directory, c2cfg, cfg["network"]["ppo_labels"])
        log(f"[input] directory: {len(directory.rows):,} rows, {len(directory.by_npi):,} NPIs with a network side")
    # An earlier PPO check table (default: this output folder's c4_ppo_check.csv) stands in for the site:
    # its listings are directory evidence and its NPIs are not searched again.
    base_rows = list(csv.DictReader(open(args.directory, encoding="utf-8"))) if args.directory and \
        str(args.directory).lower().endswith(".csv") else []
    prior_cov = {}
    csv_in = args.ppo_check_csv or (str(out / "c4_ppo_check.csv") if not args.no_ppo_check_csv else None)
    if csv_in and Path(csv_in).exists():
        site_rows, prior_cov = ppo_check.from_csv(csv_in)
        base_rows += site_rows
        if base_rows:
            used = out / "c4_directory_used.csv"
            dircheck.write_table(used, base_rows)
            directory = load_directory(used, c2cfg, cfg["network"]["ppo_labels"])
        log(f"[input] PPO check table {csv_in}: {len(prior_cov):,} NPIs already searched on the site, "
            f"{len(site_rows):,} listings reused (no new search for them)")
    elif args.ppo_check_csv:
        log(f"[input] PPO check table {args.ppo_check_csv} not found; ignored")

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
            row["oon_files"] = _read_oon(con, file_id, t.get("oon_files") or [], cfg, log)
            if args.check_directory:
                directory = _check_directory(args, con, file_id, meta, cfg, c2cfg, npis, directory, out, log,
                                             base_rows, prior_cov)
            t1 = time.time()
            network_reports[url] = tag_references(con, file_id, meta, cfg, c2cfg, directory, log)
            t2 = time.time()
            pairs = build_pairs(con, file_id, cfg["network"]["include_statuses"], log, npis=npis)
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
    tag_summary = [{"rate_file_url": u, "kind": "status", "value": k, "references": v}
                   for u, rep in network_reports.items() for k, v in rep["statuses"].items()]
    tag_summary += [{"rate_file_url": u, "kind": "tag/confidence", "value": k, "references": v}
                    for u, rep in network_reports.items() for k, v in rep["tags"].items()]
    hyp = [{"rate_file_url": u, "hypothesis": h, **{k: v for k, v in rep[h].items()}}
           for u, rep in network_reports.items() for h in ("dual_contract", "propagation")]
    picks = [{"rate_file_url": u, "kind": kind, "key": k, "count": v} for u, rep in extract_reports.items()
             for kind in ("pick_rules", "dropped") for k, v in rep[kind].items()]
    write_csv(out / "c4_extraction_log.csv", picks, ["rate_file_url", "kind", "key", "count"])
    check_rows = []
    checks = (args.directory_checks or []) + (dircheck.coverage_paths(Path(args.check_out)) if args.check_directory else [])
    if directory or checks or prior_cov or npis is not None:
        check_rows = ppo_check.build_rows(con, directory, {**prior_cov, **ppo_check.read_coverage(checks)}, npis)
        ppo_check.write(out / "c4_ppo_check.csv", check_rows)
        verdicts = Counter(r["verdict"] for r in check_rows)
        log(f"[c4]   PPO check table: {len(check_rows):,} NPI-TIN rows -> {out / 'c4_ppo_check.csv'}; "
            + ", ".join(f"{k} {v:,}" for k, v in verdicts.most_common()))
    write_csv(out / "c4_hypotheses.csv", hyp)
    write_xlsx(out / "c4_results.xlsx", {
        "files": (summary_files, None), "network_tags": (tag_summary, None), "hypotheses": (hyp, None),
        "extraction_log": (picks, None), "rates_sample": (sample, RATE_FIELDS),
    })
    summary = {
        "version": __version__, "started": started.isoformat(), "finished": datetime.now(timezone.utc).isoformat(),
        "config": cfg["_path"], "as_of": as_of, "db": str(db_path), "source": source,
        "code_scope": len(codes) if codes is not None else "all", "directory": bool(directory),
        "npi_limit": len(npis) if npis is not None else None,
        "ppo_check": dict(Counter(r["verdict"] for r in check_rows)) if check_rows else None,
        "network_status": {st: sum(rep["statuses"].get(st, 0) for rep in network_reports.values()) for st in STATUSES},
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


def _check_directory(args, con, file_id, meta, cfg, c2cfg, npis, directory, out, log, base_rows=(), prior_cov=None):
    """Search the file's NPIs on Health Net's site (see dircheck), saving c4_ppo_check.csv every
    --save-every NPIs. Returns the directory to tag the references with (the given one plus the site's)."""
    base = Path(args.check_out)
    table = base.parent / (base.name + "_directory.csv")
    given, prior_cov = list(base_rows), prior_cov or {}
    reader = dircheck.CaptureRows(base)
    state = {"directory": directory}

    def checkpoint():
        rows = reader.update()
        if not rows and not given:
            return
        dircheck.write_table(table, given + rows)
        state["directory"] = load_directory(table, c2cfg, cfg["network"]["ppo_labels"])
        tag_references(con, file_id, meta, cfg, c2cfg, state["directory"], lambda *_: None)
        con.commit()
        done = dircheck.checked_npis(dircheck.coverage_paths(base)) | set(prior_cov)
        cov = {**prior_cov, **ppo_check.read_coverage((args.directory_checks or []) + dircheck.coverage_paths(base))}
        rows_out = ppo_check.build_rows(con, state["directory"], cov, done if npis is None else done & npis)
        ppo_check.write(out / "c4_ppo_check.csv", rows_out)

    todo_all = dircheck.targets(con, npis)
    done = dircheck.checked_npis(dircheck.coverage_paths(base)) | set(prior_cov)
    todo = [t for t in todo_all if t["npi"] not in done]
    log(f"[c4]   directory check on Health Net Find a Provider ({args.check_network!r}, {args.check_year or 'this year'}): "
        f"{len(todo_all):,} NPIs, {len(todo_all) - len(todo):,} already searched, {len(todo):,} to go with "
        f"{args.check_workers} browser(s); c4_ppo_check.csv saved every {args.save_every} NPIs")
    n = dircheck.run(todo, base, args.check_workers, args.save_every, checkpoint, log,
                     network=args.check_network, year=args.check_year, executable=args.browser, channel=args.channel,
                     delay=args.check_delay, fallback=not args.no_fallback)
    log(f"[c4]   directory check done: {n:,} NPIs searched in all; directory table {table}")
    return state["directory"]


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
    r.add_argument("--npis", nargs="+", help="only these NPIs (NPIs, a text file, or a CSV with an npi column, "
                   "e.g. ptd_dir's ppo_check_results.csv): a trial run; leave out for the whole file")
    r.add_argument("--directory-checks", nargs="+", help="ptd_dir check coverage logs (<out>.coverage.jsonl, globs "
                   "allowed): which NPIs were searched on the site, where, and the name/TIN fallback")
    r.add_argument("--check-directory", action="store_true",
                   help="search every NPI of the rate file (or --npis) on Health Net's Find a Provider site and use "
                        "what it lists as network evidence; resumable")
    r.add_argument("--ppo-check-csv", help="reuse this earlier c4_ppo_check.csv: its site results are evidence and its "
                   "NPIs are not searched again (default: <out>/c4_ppo_check.csv when it exists)")
    r.add_argument("--no-ppo-check-csv", action="store_true", help="ignore an existing c4_ppo_check.csv")
    r.add_argument("--check-out", default="data/directory/hn_check_all",
                   help="base path for the site's captures and coverage logs (<base>_w<k>.jsonl)")
    r.add_argument("--check-workers", type=int, default=3, help="browsers searching side by side (default 3)")
    r.add_argument("--save-every", type=int, default=50, help="rewrite c4_ppo_check.csv every N NPIs (default 50)")
    r.add_argument("--check-network", default="PPO - Large Group/Small Group", help="network option to choose (substring)")
    r.add_argument("--check-year", help="network year (default: this year)")
    r.add_argument("--check-delay", type=float, default=1.0, help="seconds between searches per browser")
    r.add_argument("--no-fallback", action="store_true", help="do not search missed NPIs by their TIN's name")
    r.add_argument("--browser", help="path to a Chromium-based browser executable")
    r.add_argument("--channel", help="chrome or msedge (default: try both, then Playwright's Chromium)")
    r.add_argument("--db", default=str(DEFAULT_DB), help=f"rate store (default {DEFAULT_DB})")
    r.add_argument("--oon", nargs="*", help="with --files: allowed-amount (out-of-network) files to read as evidence")
    r.add_argument("--oon-ppo-only", action="store_true", help="with --oon: those files are listed only by PPO plans")
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
