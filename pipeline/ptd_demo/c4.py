"""Temporary demo runner for C4 using the reviewed PPO roster CSV.

It still reads the real PTD rate file for negotiated rates. The roster CSV labels
exact NPI+TIN pairs for demonstration; it is not itself a rate file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ptd_c2.config import load_config as load_c2_config
from ptd_c3.codes import load_expected_codes
from ptd_c4 import cli as base
from ptd_c4.config import load_config
from ptd_c4.db import DEFAULT_DB, connect, drop_c4, finish_c4, init_c4
from ptd_c4.directory import load_directory
from ptd_c4.extract import build_pairs, fanout_counts, pick_rates, tag_table_rows
from ptd_c4.ingest import ingest_file
from ptd_c4.network import STATUSES, CONFIRMED_PPO
from .roster import DEFAULT_DEMO_CSV, demo_pair_status, load_demo_rows

DEFAULT_RATE_FILE = "https://centene-provider-transparency.s3.amazonaws.com/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json.gz"
RATE_FIELDS, TAG_FIELDS = base.RATE_FIELDS, base.TAG_FIELDS


def _apply_demo_statuses(con, file_id: int, pair_status: dict, log) -> dict:
    """Only exact NPI+TIN matches are confirmed. NOT_FOUND remains unresolved, never NON_PPO."""
    con.execute(
        "UPDATE c4_refs SET tag='UNKNOWN', tag_confidence='none', ppo_evidence=0, non_ppo_evidence=0, "
        "evidence='demo roster: no exact NPI+TIN evidence', status='UNRESOLVED', "
        "status_basis='not in demo roster' WHERE file_id=?", (file_id,))
    counts = {"confirmed_ppo": 0, "unresolved": 0, "missing_pair": 0}
    for (npi, tin), status in pair_status.items():
        refs = con.execute(
            "SELECT ref_id FROM c4_ref_members WHERE file_id=? AND npi=? AND tin=?",
            (file_id, npi, tin)).fetchall()
        if not refs:
            counts["missing_pair"] += 1
            continue
        for (ref_id,) in refs:
            if status == CONFIRMED_PPO:
                con.execute(
                    "UPDATE c4_refs SET tag='PPO', tag_confidence='high', ppo_evidence=1, non_ppo_evidence=0, "
                    "evidence='demo roster verdict: PPO found on Health Net directory', "
                    "status='CONFIRMED_PPO', status_basis='temporary demo PPO roster: exact NPI+TIN' "
                    "WHERE file_id=? AND ref_id=?", (file_id, ref_id))
                counts["confirmed_ppo"] += 1
            else:
                con.execute(
                    "UPDATE c4_refs SET tag='UNKNOWN', tag_confidence='none', ppo_evidence=0, non_ppo_evidence=0, "
                    "evidence='demo roster verdict: not found; not treated as non-PPO', status='UNRESOLVED', "
                    "status_basis='temporary demo roster: not found is unresolved' "
                    "WHERE file_id=? AND ref_id=?", (file_id, ref_id))
                counts["unresolved"] += 1
    con.commit()
    log(f"[demo] applied exact NPI+TIN statuses: {counts}")
    return counts


def run(args) -> int:
    cfg = load_config(args.config, args.as_of)
    c2cfg = load_c2_config(args.c2_config)
    cfg["codes"]["scope"] = args.codes
    if args.workers is not None:
        cfg["stream"]["workers"] = args.workers
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    rows = load_demo_rows(args.demo_csv)
    pair_status = demo_pair_status(args.demo_csv)
    url = args.rate_file
    log(f"C4 DEMO: {len(rows)} roster rows, {len(pair_status)} unique NPI+TIN pairs, rate file={url}")
    codes = sorted(load_expected_codes(args.workbook).codes) if args.codes == "workbook" and args.workbook else None
    if args.codes == "workbook" and not args.workbook:
        raise SystemExit("--codes workbook requires --workbook")
    directory = load_directory(args.directory, c2cfg, cfg["network"]["ppo_labels"]) if args.directory else None
    if directory:
        log(f"[input] loaded {len(directory.rows):,} supplemental payer-directory rows")
    db = Path(args.db)
    con = connect(db)
    drop_c4(con)
    init_c4(con)
    file_row = {"file_id": 1, "url": url, "from_c2": False, "needs_network_filter": True,
                "c2_network_label": "DEMO_ROSTER", "c2_ppo_network_names": "Health Net PPO"}
    report = {}
    status = 0
    try:
        info = ingest_file(con, db, 1, url, cfg, codes, log, args.max_items)
        file_row.update(local_path=info["local_path"], status="ok", items=info["items"],
                        items_in_scope=info["items_in_scope"], price_rows=info["price_rows"], seconds=info["seconds"],
                        all_price_rows=info["all_price_rows"], distinct_codes=info["distinct_codes"],
                        workbook_codes_found=info["workbook_codes_found"], code_types=info["code_types"],
                        references=info["refs"], size_bytes=Path(info["local_path"]).stat().st_size,
                        **{k: info["top"].get(k) for k in ("last_updated_on", "version", "reporting_entity_name")})
        _apply_demo_statuses(con, 1, pair_status, log)
        pair_stats = build_pairs(con, 1, cfg["network"]["include_statuses"], log)
        price_stats = pick_rates(con, 1, cfg, cfg["_as_of"].isoformat(), log)
        report = {**pair_stats, **price_stats, **fanout_counts(con, 1)}
    except Exception as exc:
        status = 2
        file_row.update(status="error", error=f"{exc.__class__.__name__}: {exc}"[:500])
        log(f"[c4] ERROR {file_row['error']}")
    con.execute(
        "INSERT INTO c4_files (file_id, url, local_path, last_updated_on, version, reporting_entity_name, "
        "needs_network_filter, c2_network_label, c2_ppo_network_names, c3_score, c3_grade, status, error, "
        "items, items_in_scope, price_rows, seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(file_row.get(k) for k in ("file_id", "url", "local_path", "last_updated_on", "version",
                                       "reporting_entity_name", "needs_network_filter", "c2_network_label",
                                       "c2_ppo_network_names", "c3_score", "c3_grade", "status", "error",
                                       "items", "items_in_scope", "price_rows", "seconds")))
    finish_c4(con)
    tags = [dict(zip(TAG_FIELDS, r)) for r in con.execute(f"SELECT {', '.join(TAG_FIELDS)} FROM c4_refs")]
    base.write_csv(out / "c4_network_tags.csv", tags, TAG_FIELDS)
    base.write_csv(out / "c4_provider_tags.csv", tag_table_rows(con, 1), ["npi", "tin", "tag"])
    rates = [dict(zip(RATE_FIELDS, r)) for r in con.execute(f"SELECT {', '.join(RATE_FIELDS)} FROM c4_rates LIMIT 5000")]
    base.write_xlsx(out / "c4_results.xlsx", {"files": ([file_row], None), "network_tags": (tags, None),
                                             "rates_sample": (rates, RATE_FIELDS)})
    summary = {
        "demo_mode": True, "demo_roster": str(args.demo_csv), "source": url, "files": [file_row],
        "network_status": {st: con.execute("SELECT COUNT(*) FROM c4_refs WHERE status=?", (st,)).fetchone()[0]
                           for st in STATUSES},
        "extraction": {url: report}, "totals": report, "distinct_npis": con.execute(
            "SELECT COUNT(DISTINCT npi) FROM c4_pairs WHERE sig_id IN (SELECT sig_id FROM c4_sig_rates)").fetchone()[0],
        "distinct_tins": con.execute(
            "SELECT COUNT(DISTINCT tin) FROM c4_pairs WHERE sig_id IN (SELECT sig_id FROM c4_sig_rates)").fetchone()[0],
        "demo_roster_rows": len(rows), "demo_pairs": len(pair_status),
        "demo_verdicts": {v: sum(r["verdict"] == v for r in rows) for v in sorted({r["verdict"] for r in rows})},
    }
    (out / "c4_run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "c4_run_log.txt").write_text("\n".join(lines) + "\n")
    con.close()
    log(f"[done] C4 demo outputs: {out}; database: {db}")
    return status


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo-csv", default=str(DEFAULT_DEMO_CSV))
    ap.add_argument("--rate-file", default=DEFAULT_RATE_FILE, help="actual PTD JSON/JSON.GZ file or URL")
    ap.add_argument("--workbook")
    ap.add_argument("--codes", choices=["all", "workbook"], default="all")
    ap.add_argument("--directory", help="optional directory CSV/XLSX for supplemental C5 address evidence")
    ap.add_argument("--db", default="out/demo/rates.sqlite")
    ap.add_argument("--out", default="out/demo/c4")
    ap.add_argument("--config")
    ap.add_argument("--c2-config")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--max-items", type=int)
    ap.add_argument("--as-of")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
