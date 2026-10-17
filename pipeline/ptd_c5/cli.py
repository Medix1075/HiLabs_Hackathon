"""Command line entry point.

  python -m ptd_c5 run --nppes NPPES.zip --dac DAC_NationalDownloadableFile.csv     # addresses for C4's pairs
  python -m ptd_c5 run --nppes data/nppes_ca.csv --nppes-pl data/nppes_pl_ca.csv --directory data/directory.csv
  python -m ptd_c5 run --npi-api 500           # no NPPES file: look up at most 500 NPIs live
  python -m ptd_c5 fetch --dest data/public     # download NPPES, Doctors & Clinicians, NUCC
  python -m ptd_c5 extract-nppes --nppes data/public/NPPES_Data_Dissemination_....zip --state CA
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ptd_c2.cli import write_csv, write_xlsx
from ptd_c2.config import load_config as load_c2_config
from ptd_c4.config import load_config as load_c4_config
from ptd_c4.db import DEFAULT_DB, connect, table_exists
from ptd_c4.directory import load_directory

from . import __version__
from .config import load_config
from .fetch import extract_nppes, fetch, read_zip_county
from .logic import write_logic
from .address import standardize
from .resolve import Resolver
from .sources import directory_candidates, npi_api_lookup, read_dac, read_nppes, read_nppes_pl

ADDRESS_FIELDS = ["npi", "tin", "rank", "address", "line1", "unit", "city", "state", "zip5", "county", "region",
                  "confidence", "verified", "families", "sources", "tin_consensus", "source_p", "evidence", "site_key"]
CANDIDATE_FIELDS = ["npi", "tin", "site_key", "address", "confidence", "source_p", "tin_consensus", "families",
                    "sources", "verified", "evidence"]
PROVIDER_FIELDS = ["npi", "entity_type", "name", "primary_taxonomy", "taxonomies", "dac_specialty", "dac_groups",
                   "nppes_last_update", "deactivated", "source"]


def run(args) -> int:
    cfg = load_config(args.config, args.as_of)
    if args.npi_api is not None:
        cfg["npi_api"]["max_lookups"] = args.npi_api
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    started = datetime.now(timezone.utc)
    log(f"C5 v{__version__} started {started.isoformat()} config={cfg['_path']} as_of={cfg['_as_of']}")
    con = connect(args.db)
    if not table_exists(con, "c4_pairs"):
        print(f"No C4 rate store in {args.db}. Run C4 first (python -m ptd_c4 run).", file=sys.stderr)
        return 1
    pairs = con.execute("SELECT DISTINCT npi, tin FROM c4_pairs WHERE sig_id IN (SELECT DISTINCT sig_id FROM "
                        "c4_sig_rates) ORDER BY npi, tin").fetchall()
    npis = {p[0] for p in pairs}
    log(f"[input] {len(pairs):,} NPI-TIN pairs with rates, {len(npis):,} NPIs, "
        f"{len({p[1] for p in pairs}):,} TINs (from {args.db})")

    providers, dac_facts, cands, used = {}, {}, [], {}
    if args.nppes:
        p, c = read_nppes(args.nppes, npis, log)
        providers.update(p)
        cands += c
        used["nppes"] = args.nppes
        pl = args.nppes_pl
        if not pl and zipfile.is_zipfile(args.nppes) and \
                any("pl_pfile" in n.lower() for n in zipfile.ZipFile(args.nppes).namelist()):
            pl = args.nppes
        if pl:
            dates = {n: datetime.fromisoformat(v["nppes_last_update"]).date() if v["nppes_last_update"] else None
                     for n, v in providers.items()}
            cands += read_nppes_pl(pl, npis, dates, log)
            used["nppes_pl"] = pl
    if args.dac:
        dac_facts, c = read_dac(args.dac, npis, cfg["_as_of"], log)
        cands += c
        used["dac"] = args.dac
    if args.directory:
        c4cfg = load_c4_config()
        directory = load_directory(args.directory, load_c2_config(), c4cfg["network"]["ppo_labels"])
        c = directory_candidates(directory, npis, cfg["_as_of"])
        cands += c
        used["directory"] = args.directory
        log(f"[c5]   directory: {len(c):,} addresses for our NPIs")
    if cfg["npi_api"]["max_lookups"]:
        missing = sorted(npis - set(providers))
        p, c = npi_api_lookup(missing, cfg, out / "npi_api_cache.jsonl", log)
        providers.update(p)
        cands += c
        used["npi_api"] = f"{len(p)} NPIs"
    if not used:
        log("[input] no address source given (--nppes, --dac, --directory, --npi-api): every pair stays unresolved. "
            "See `python -m ptd_c5 fetch`.")
    zip_county = read_zip_county(args.zip_county) if args.zip_county else {}
    if zip_county:
        log(f"[input] ZIP to county: {len(zip_county):,} ZIPs")
        used["zip_county"] = args.zip_county

    res = Resolver(cfg, cfg["_as_of"], providers, dac_facts, zip_county)
    n = res.add_candidates(cands)
    log(f"[c5] {n:,} candidate addresses standardised into {len(res.addr):,} distinct sites")
    res.build_tin_view(pairs)

    kept_all, cand_rows = [], []
    for npi, tin in pairs:
        kept, scored = res.resolve_pair(npi, tin)
        kept_all += kept
        if cfg["outputs"]["candidates_csv"]:
            cand_rows += scored

    prov_rows = []
    for npi in sorted(npis):
        p = dict(providers.get(npi) or {"npi": npi, "entity_type": "", "name": "", "primary_taxonomy": "",
                                        "taxonomies": "", "nppes_last_update": "", "deactivated": False, "source": ""})
        d = dac_facts.get(npi)
        if d:
            p["dac_specialty"], p["dac_groups"] = d["pri_spec"], "|".join(sorted(d["pacs"]))
            p["entity_type"] = p["entity_type"] or "1"  # Doctors & Clinicians lists individuals only
            p["name"] = p["name"] or d["name"]
        prov_rows.append(p)

    # ---------------------------------------------------------------- store + files
    con.execute("DROP TABLE IF EXISTS c5_addresses")
    con.execute(f"CREATE TABLE c5_addresses ({', '.join(ADDRESS_FIELDS)})")
    con.executemany(f"INSERT INTO c5_addresses VALUES ({','.join('?' * len(ADDRESS_FIELDS))})",
                    [tuple(r.get(k, "") for k in ADDRESS_FIELDS) for r in kept_all])
    con.execute("CREATE INDEX ix_c5_addr ON c5_addresses (npi, tin)")
    con.execute("DROP TABLE IF EXISTS c5_providers")
    con.execute(f"CREATE TABLE c5_providers ({', '.join(PROVIDER_FIELDS)}, PRIMARY KEY (npi))")
    con.executemany(f"INSERT INTO c5_providers VALUES ({','.join('?' * len(PROVIDER_FIELDS))})",
                    [tuple(r.get(k, "") for k in PROVIDER_FIELDS) for r in prov_rows])
    con.commit()

    tin_sites = []
    for tin, cnt in res.tin_sites.items():
        for site, k in cnt.most_common(10):
            tin_sites.append({"tin": tin, "site_key": site, "address": res.addr[site].display(), "roster_npis": k,
                              "roster_with_address": res.tin_with_any[tin], "roster_size": len(res.roster[tin])})
    write_csv(out / "c5_addresses.csv", kept_all, ADDRESS_FIELDS)
    write_csv(out / "c5_providers.csv", prov_rows, PROVIDER_FIELDS)
    write_csv(out / "c5_tin_sites.csv", tin_sites, ["tin", "site_key", "address", "roster_npis",
                                                     "roster_with_address", "roster_size"])
    if cfg["outputs"]["candidates_csv"]:
        write_csv(out / "c5_candidates.csv", cand_rows, CANDIDATE_FIELDS)

    best = [r for r in kept_all if r["rank"] == 1]
    # The baseline the brief warns about: NPPES's own practice address. How often do we keep it?
    nppes_site = {}
    for c in cands:
        if c["source"] == "nppes_practice" and c.get("line1"):
            nppes_site[c["npi"]] = standardize(c["line1"], c.get("line2", ""), c.get("city", ""), c.get("state", ""),
                                               c.get("zip", "")).site_key
    vs_nppes = Counter("no NPPES address" if r["npi"] not in nppes_site else
                       "same as NPPES" if nppes_site[r["npi"]] == r["site_key"] else "differs from NPPES"
                       for r in best if r["address"])
    resolved = [r for r in best if r["address"]]
    npi_verified = {r["npi"] for r in kept_all if r["verified"]}
    npi_resolved = {r["npi"] for r in resolved}
    stats = {
        "pairs": len(pairs), "npis": len(npis), "pairs_with_address": len(resolved),
        "pairs_with_verified_address": sum(1 for r in best if r["verified"]),
        "npis_with_address": len(npi_resolved), "npis_with_verified_address": len(npi_verified),
        "pct_npis_with_address": round(100 * len(npi_resolved) / len(npis), 2) if npis else 0,
        "pct_npis_with_verified_address": round(100 * len(npi_verified) / len(npis), 2) if npis else 0,
        "address_rows": len(kept_all), "multi_site_pairs": sum(1 for r in kept_all if r["rank"] == 2),
        "unresolved_reasons": dict(Counter(r["evidence"] for r in best if not r["address"])),
        "best_by_families": dict(Counter(r["families"] for r in resolved).most_common(15)),
        "confidence_bands": dict(Counter(("0.8+" if r["confidence"] >= 0.8 else "0.6-0.8" if r["confidence"] >= 0.6
                                          else "0.4-0.6" if r["confidence"] >= 0.4 else "<0.4") for r in resolved)),
        "best_address_vs_nppes_practice": dict(vs_nppes),
        "sources_used": used,
    }
    for k in ("pct_npis_with_address", "pct_npis_with_verified_address"):
        log(f"[c5] {k}: {stats[k]}%")
    write_xlsx(out / "c5_results.xlsx", {"addresses": (kept_all[:200000], ADDRESS_FIELDS),
                                          "providers": (prov_rows[:200000], PROVIDER_FIELDS),
                                          "tin_sites": (tin_sites[:200000], None),
                                          "stats": ([{"metric": k, "value": json.dumps(v) if isinstance(v, dict) else v}
                                                     for k, v in stats.items()], None)})
    write_logic(out / "c5_logic.md", cfg, stats, kept_all, cand_rows)
    summary = {"version": __version__, "started": started.isoformat(),
               "finished": datetime.now(timezone.utc).isoformat(), "config": cfg["_path"], "as_of": str(cfg["_as_of"]),
               **stats}
    (out / "c5_run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "c5_run_log.txt").write_text("\n".join(log_lines) + "\n")
    con.close()
    log(f"[done] outputs in {out} (logic file: {out / 'c5_logic.md'})")
    return 0


def fetch_cmd(args) -> int:
    got = fetch(args.what, Path(args.dest))
    print(json.dumps(got, indent=2))
    return 0 if got else 1


def extract_cmd(args) -> int:
    extract_nppes(args.nppes, args.state, Path(args.out))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ptd_c5", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="resolve the practice address(es) of every NPI-TIN pair")
    r.add_argument("--db", default=str(DEFAULT_DB), help=f"rate store written by C4 (default {DEFAULT_DB})")
    r.add_argument("--nppes", help="NPPES main file: the monthly ZIP, its npidata CSV, or a state extract")
    r.add_argument("--nppes-pl", help="NPPES Practice Location Reference File (found inside the ZIP if not given)")
    r.add_argument("--dac", help="CMS Doctors and Clinicians National Downloadable File (CSV)")
    r.add_argument("--directory", help="payer directory table (CSV/XLSX with npi and address columns)")
    r.add_argument("--npi-api", type=int, help="look up at most N NPIs missing from the files in the NPI Registry API")
    r.add_argument("--zip-county", help="ZIP to county CSV (gives C7 county-level peer groups)")
    r.add_argument("--config", help="config YAML (default config/c5.yaml)")
    r.add_argument("--as-of", help="date for recency (YYYY-MM-DD); default today")
    r.add_argument("--out", default="out/c5")
    r.set_defaults(func=run)
    f = sub.add_parser("fetch", help="download NPPES, CMS Doctors & Clinicians and NUCC taxonomy")
    f.add_argument("--what", nargs="+", default=["nppes", "dac", "nucc"], choices=["nppes", "dac", "nucc"])
    f.add_argument("--dest", default="data/public")
    f.set_defaults(func=fetch_cmd)
    e = sub.add_parser("extract-nppes", help="cut the NPPES monthly ZIP down to one state")
    e.add_argument("--nppes", required=True)
    e.add_argument("--state", default="CA")
    e.add_argument("--out", default="data")
    e.set_defaults(func=extract_cmd)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
