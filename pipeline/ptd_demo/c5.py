"""Temporary C5 demo runner.

Unlike production C5, the roster in ppo_check_results.csv defines which exact
NPI+TIN pairs are included. This permits demo resolution even when a pair has
not survived price-row extraction. Address sources retain their usual scores.
"""
from __future__ import annotations

import argparse
import csv
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
from ptd_c5.config import load_config
from ptd_c5.fetch import fetch, extract_nppes, read_zip_county
from ptd_c5.logic import write_logic
from ptd_c5.address import standardize
from ptd_c5.resolve import Resolver
from ptd_c5.sources import directory_candidates, npi_api_lookup, read_dac, read_nppes, read_nppes_pl
from .roster import DEFAULT_DEMO_CSV, demo_pairs, load_demo_rows

ADDRESS_FIELDS = ["npi", "tin", "rank", "address", "line1", "unit", "city", "state", "zip5", "county", "region",
                  "confidence", "verified", "families", "sources", "tin_consensus", "source_p", "evidence", "site_key"]
CANDIDATE_FIELDS = ["npi", "tin", "site_key", "address", "confidence", "source_p", "tin_consensus", "families",
                    "sources", "verified", "evidence"]
PROVIDER_FIELDS = ["npi", "entity_type", "name", "primary_taxonomy", "taxonomies", "dac_specialty", "dac_groups",
                   "nppes_last_update", "deactivated", "source"]


def run(args) -> int:
    cfg = load_config(args.config, args.as_of)
    if args.npi_api is not None:
        v = str(args.npi_api).lower()
        if v.isdigit():
            cfg["npi_api"]["mode"], cfg["npi_api"]["max_lookups"] = ("auto" if int(v) else "off"), int(v)
        else:
            cfg["npi_api"]["mode"] = v
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    logs = []
    def log(msg):
        print(msg, flush=True); logs.append(msg)

    rows = load_demo_rows(args.demo_csv)
    pairs = sorted(set(demo_pairs(args.demo_csv)))
    npis = {npi for npi, _ in pairs}
    log(f"C5 DEMO: {len(rows)} CSV rows; {len(pairs)} unique NPI+TIN pairs; {len(npis)} unique NPIs; "
        f"{len({tin for _, tin in pairs})} TINs")
    con = connect(args.db)
    if not table_exists(con, "c4_pairs"):
        log("[demo] C4 database has not been created yet; addresses will be resolved using CSV roster directly.")
    providers, dac_facts, cands, used = {}, {}, [], {}
    if args.nppes:
        p, c = read_nppes(args.nppes, npis, log); providers.update(p); cands += c; used["nppes"] = args.nppes
        pl = args.nppes_pl
        if not pl and zipfile.is_zipfile(args.nppes) and any("pl_pfile" in n.lower() for n in zipfile.ZipFile(args.nppes).namelist()):
            pl = args.nppes
        if pl:
            dates = {n: datetime.fromisoformat(v["nppes_last_update"]).date() if v.get("nppes_last_update") else None
                     for n, v in providers.items()}
            cands += read_nppes_pl(pl, npis, dates, log); used["nppes_pl"] = pl
    if args.dac:
        dac_facts, c = read_dac(args.dac, npis, cfg["_as_of"], log); cands += c; used["dac"] = args.dac
    if args.directory:
        c4cfg = load_c4_config()
        directory = load_directory(args.directory, load_c2_config(), c4cfg["network"]["ppo_labels"])
        c = directory_candidates(directory, npis, cfg["_as_of"]); cands += c; used["directory"] = args.directory
        log(f"[c5] payer directory: {len(c):,} address candidates")
    mode = cfg["npi_api"]["mode"]
    if mode in ("auto", "always") and cfg["npi_api"]["max_lookups"]:
        ask = sorted(npis) if mode == "always" else sorted(npis - set(providers))
        if ask:
            p, c = npi_api_lookup(ask, cfg, Path(cfg["npi_api"]["cache_path"]), log)
            providers.update(p); cands += c
            if p: used["npi_api"] = f"{len(p)} NPIs"
    if not used:
        log("[input] no address source used; pairs will remain unresolved (no address is fabricated).")
    zip_county = read_zip_county(args.zip_county) if args.zip_county else {}
    if zip_county: used["zip_county"] = args.zip_county
    resolver = Resolver(cfg, cfg["_as_of"], providers, dac_facts, zip_county)
    n_cands = resolver.add_candidates(cands)
    resolver.build_tin_view(pairs)
    kept_all, candidate_rows = [], []
    for npi, tin in pairs:
        kept, scored = resolver.resolve_pair(npi, tin)
        kept_all += kept
        if cfg["outputs"]["candidates_csv"]: candidate_rows += scored

    provider_rows = []
    for npi in sorted(npis):
        p = dict(providers.get(npi) or {"npi": npi, "entity_type": "", "name": "", "primary_taxonomy": "",
              "taxonomies": "", "nppes_last_update": "", "deactivated": False, "source": ""})
        d = dac_facts.get(npi)
        if d:
            p["dac_specialty"], p["dac_groups"] = d["pri_spec"], "|".join(sorted(d["pacs"]))
            p["entity_type"] = p["entity_type"] or "1"; p["name"] = p["name"] or d["name"]
        provider_rows.append(p)

    # Replace C5's tables with the demo pairs (not only pairs whose rates survived C4 cleaning).
    con.execute("DROP TABLE IF EXISTS c5_addresses")
    con.execute(f"CREATE TABLE c5_addresses ({', '.join(ADDRESS_FIELDS)})")
    con.executemany(f"INSERT INTO c5_addresses VALUES ({','.join('?' * len(ADDRESS_FIELDS))})",
                    [tuple(r.get(k, "") for k in ADDRESS_FIELDS) for r in kept_all])
    con.execute("CREATE INDEX ix_c5_demo_addr ON c5_addresses (npi, tin)")
    con.execute("DROP TABLE IF EXISTS c5_providers")
    con.execute(f"CREATE TABLE c5_providers ({', '.join(PROVIDER_FIELDS)}, PRIMARY KEY (npi))")
    con.executemany(f"INSERT INTO c5_providers VALUES ({','.join('?' * len(PROVIDER_FIELDS))})",
                    [tuple(r.get(k, "") for k in PROVIDER_FIELDS) for r in provider_rows])
    con.commit()

    tin_sites = []
    for tin, cnt in resolver.tin_sites.items():
        for site, k in cnt.most_common(10):
            tin_sites.append({"tin": tin, "site_key": site, "address": resolver.addr[site].display(), "roster_npis": k,
                              "roster_with_address": resolver.tin_with_any[tin], "roster_size": len(resolver.roster[tin])})
    write_csv(out / "c5_addresses.csv", kept_all, ADDRESS_FIELDS)
    write_csv(out / "c5_providers.csv", provider_rows, PROVIDER_FIELDS)
    write_csv(out / "c5_tin_sites.csv", tin_sites, ["tin", "site_key", "address", "roster_npis", "roster_with_address", "roster_size"])
    if cfg["outputs"]["candidates_csv"]: write_csv(out / "c5_candidates.csv", candidate_rows, CANDIDATE_FIELDS)

    best = [r for r in kept_all if r["rank"] == 1]
    nppes_sites = {}
    for c in cands:
        if c["source"] in ("nppes_practice", "npi_api_practice") and c.get("line1"):
            nppes_sites[c["npi"]] = standardize(c["line1"], c.get("line2", ""), c.get("city", ""), c.get("state", ""), c.get("zip", "")).site_key
    compare = Counter("no NPPES address" if r["npi"] not in nppes_sites else
                      "same as NPPES" if nppes_sites[r["npi"]] == r["site_key"] else "differs from NPPES"
                      for r in best if r["address"])
    resolved = [r for r in best if r["address"]]
    npi_verified = {r["npi"] for r in kept_all if r["verified"]}
    tins = {tin for _, tin in pairs}
    stats = {
        "demo_mode": True, "demo_roster": str(args.demo_csv), "pairs": len(pairs), "npis": len(npis), "tins": len(tins),
        "pairs_with_address": len(resolved), "pairs_with_verified_address": sum(bool(r["verified"]) for r in best),
        "npis_with_address": len({r["npi"] for r in resolved}), "npis_with_verified_address": len(npi_verified),
        "pct_npis_with_address": round(100 * len({r["npi"] for r in resolved}) / len(npis), 2) if npis else 0,
        "pct_npis_with_verified_address": round(100 * len(npi_verified) / len(npis), 2) if npis else 0,
        "tins_with_address": len({r["tin"] for r in resolved}),
        "tins_with_verified_address": len({r["tin"] for r in kept_all if r["verified"]}),
        "pct_tins_with_address": round(100 * len({r["tin"] for r in resolved}) / len(tins), 2) if tins else 0,
        "pct_tins_with_verified_address": round(100 * len({r["tin"] for r in kept_all if r["verified"]}) / len(tins), 2) if tins else 0,
        "address_rows": len(kept_all), "candidate_addresses": n_cands,
        "multi_site_pairs": sum(r["rank"] == 2 for r in kept_all),
        "unresolved_reasons": dict(Counter(r["evidence"] for r in best if not r["address"])),
        "best_by_families": dict(Counter(r["families"] for r in resolved).most_common(15)),
        "confidence_bands": dict(Counter(("0.8+" if r["confidence"] >= .8 else "0.6-0.8" if r["confidence"] >= .6
                                          else "0.4-0.6" if r["confidence"] >= .4 else "<0.4") for r in resolved)),
        "best_address_vs_nppes_practice": dict(compare), "sources_used": used,
        "demo_verdicts": {v: sum(r["verdict"] == v for r in rows) for v in sorted({r["verdict"] for r in rows})},
    }
    log(f"[c5] NPIs with address: {stats['pct_npis_with_address']}%; with verified address: {stats['pct_npis_with_verified_address']}%")
    write_xlsx(out / "c5_results.xlsx", {"addresses": (kept_all[:200000], ADDRESS_FIELDS),
         "providers": (provider_rows[:200000], PROVIDER_FIELDS), "tin_sites": (tin_sites[:200000], None),
         "stats": ([{"metric": k, "value": json.dumps(v) if isinstance(v, dict) else v} for k, v in stats.items()], None)})
    write_logic(out / "c5_logic.md", cfg, stats, kept_all, candidate_rows)
    summary = {"version": "demo", "started": datetime.now(timezone.utc).isoformat(),
               "finished": datetime.now(timezone.utc).isoformat(), "config": cfg["_path"], "as_of": str(cfg["_as_of"]), **stats}
    (out / "c5_run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "c5_run_log.txt").write_text("\n".join(logs) + "\n")
    con.close()
    log(f"[done] C5 demo outputs: {out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo-csv", default=str(DEFAULT_DEMO_CSV))
    ap.add_argument("--db", default="out/demo/rates.sqlite")
    ap.add_argument("--nppes")
    ap.add_argument("--nppes-pl")
    ap.add_argument("--dac")
    ap.add_argument("--directory")
    ap.add_argument("--zip-county")
    ap.add_argument("--npi-api", default="auto")
    ap.add_argument("--config")
    ap.add_argument("--as-of")
    ap.add_argument("--out", default="out/demo/c5")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
