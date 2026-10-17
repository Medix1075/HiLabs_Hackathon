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
from ptd_c4.db import connect, drop_c4, finish_c4, init_c4
from ptd_c4.directory import load_directory
from ptd_c4.extract import build_pairs, fanout_counts, pick_rates, tag_table_rows
from ptd_c4.ingest import ingest_file
from ptd_c4.network import STATUSES, CONFIRMED_PPO
from .roster import DEFAULT_DEMO_CSV, demo_pair_status, load_demo_rows

DEFAULT_RATE_FILE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT/2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json"
RATE_FIELDS, TAG_FIELDS = base.RATE_FIELDS, base.TAG_FIELDS


def _apply_demo_statuses(con, file_id: int, pair_status: dict, log) -> None:
    """Keep reference-level evidence unresolved; the CSV verdict is pair-specific."""
    con.execute(
        "UPDATE c4_refs SET tag='UNKNOWN', tag_confidence='none', ppo_evidence=0, non_ppo_evidence=0, "
        "evidence='demo CSV is NPI+TIN-specific and cannot classify this shared provider reference by itself', "
        "status='UNRESOLVED', status_basis='pair-specific demo roster is applied after signature extraction' "
        "WHERE file_id=?", (file_id,))
    con.commit()
    log(f"[demo] kept {con.execute('SELECT COUNT(*) FROM c4_refs WHERE file_id=?', (file_id,)).fetchone()[0]:,} "
        "shared provider references unresolved; exact roster labels will be attached to NPI+TIN rate signatures.")


def _restrict_and_clone_pairs(con, file_id: int, pair_status: dict, log) -> dict:
    """Keep only exact demo NPI+TIN pairs and set their network status on cloned rates."""
    import re
    from ptd_c4.db import stable_id

    original = con.execute(
        "SELECT npi, tin, tin_type, sig_id FROM c4_pairs WHERE file_id=?", (file_id,)
    ).fetchall()
    wanted = {}
    for npi, tin, tin_type, sig_id in original:
        norm_tin = re.sub(r"\D", "", str(tin or ""))
        key = (int(npi), norm_tin)
        if key in pair_status and norm_tin:
            wanted[key] = (str(tin or ""), str(tin_type or ""), int(sig_id))
    con.execute("DELETE FROM c4_pairs WHERE file_id=?", (file_id,))
    cloned, missing_rates = 0, 0
    for (npi, norm_tin), status in pair_status.items():
        match = wanted.get((npi, norm_tin))
        if not match:
            continue
        _, tin_type, original_sig = match
        rate_rows = con.execute(
            "SELECT * FROM c4_sig_rates WHERE file_id=? AND sig_id=?",
            (file_id, original_sig),
        ).fetchall()
        if not rate_rows:
            missing_rates += 1
            continue
        new_sig = stable_id(file_id, original_sig, "demo-pair", npi, norm_tin, status)
        for values in rate_rows:
            row = list(values)
            # c4_sig_rates columns: file_id,sig_id,...,network_tag,network_confidence,
            # candidates,distinct_rates,rate_min,rate_max,pick_rule,network_status
            row[1] = new_sig
            row[11] = "PPO" if status == CONFIRMED_PPO else "UNKNOWN"
            row[12] = "high" if status == CONFIRMED_PPO else "none"
            row[17] = "demo_roster_verdict"
            row[18] = status
            con.execute(
                "INSERT INTO c4_sig_rates VALUES (" + ",".join("?" * len(row)) + ")",
                row,
            )
        # Normalize TIN formatting to the roster convention so the C5 addresses join in C6.
        con.execute(
            "INSERT INTO c4_pairs VALUES (?,?,?,?,?)",
            (file_id, npi, norm_tin, tin_type, new_sig),
        )
        cloned += 1
    con.commit()
    not_in_file = len([k for k in pair_status if k not in wanted])
    log(
        f"[demo] kept {cloned:,} exact demo NPI+TIN pairs with extracted rates; "
        f"{missing_rates:,} matched pairs had no surviving rates; "
        f"{not_in_file:,} CSV pairs had no exact pair in the PTD rate file. Non-roster pairs excluded."
    )
    return {
        "demo_pairs_with_rates": cloned,
        "demo_pairs_without_rates": missing_rates,
        "rate_file_pairs_seen": len(original),
        "demo_pairs_not_found_in_rate_file": not_in_file,
    }


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
        demo_filter_stats = _restrict_and_clone_pairs(con, 1, pair_status, log)
        report = {**pair_stats, **price_stats, **demo_filter_stats, **fanout_counts(con, 1)}
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
        "demo_pair_status_counts": {v: sum(1 for x in pair_status.values() if x == v) for v in sorted(set(pair_status.values()))},
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
