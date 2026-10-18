"""The PPO check table: what the rate file says about each NPI + TIN next to what the payer's own
directory site says (c4_ppo_check.csv).

One row per NPI x TIN of the rate file (limited to `--npis` when given). Rate-file side: the TIN's
business name, the provider references the pair sits in with their network status, and how many
rates were extracted. Directory side (Health Net Find a Provider, from `ptd_dir check`): where and
under which network the NPI was searched, whether the NPI itself was found, the listing's name,
product, address and specialty, and the name/TIN fallback for NPIs the site does not find.

`verdict`:
  FOUND_PPO_BY_NPI            the NPI is listed (Par) in the PPO network, under this TIN or none given
  FOUND_PPO_NPI_OTHER_TIN     the NPI is listed, but only under other TINs
  NOT_FOUND_BY_NPI_SAME_TIN   the NPI is not listed, but providers with the same TIN are (weak)
  NOT_FOUND                   searched, nothing for this NPI or TIN
  NOT_CHECKED                 not searched on the site
Not finding an NPI is never evidence against PPO (see ptd_dir.healthnet).
"""
from __future__ import annotations

import csv
import glob
import json
import re
from collections import defaultdict
from pathlib import Path

FIELDS = ["npi", "tin", "tin_name", "rate_file_refs", "ref_count", "ref_statuses", "pair_status", "status_basis",
          "rates_extracted", "codes_extracted", "site_checked", "search_location", "network_searched", "network_year",
          "npi_search_result", "directory_name", "directory_tin", "directory_listing", "directory_addresses",
          "directory_specialty", "tin_fallback_term", "tin_fallback_listings", "tin_fallback_names",
          "tin_fallback_npis", "verdict"]
RANK = {"CONFIRMED_PPO": 4, "PPO_CANDIDATE": 3, "UNRESOLVED": 2, "CONFIRMED_NON_PPO": 1}


def _digits(x) -> str:
    return re.sub(r"\D", "", str(x or ""))


def read_coverage(patterns: list[str]) -> dict[str, dict]:
    """NPI -> its latest coverage line from `ptd_dir check` (<out>.coverage.jsonl files)."""
    out: dict[str, dict] = {}
    for pat in patterns or []:
        for p in sorted(glob.glob(pat)) or [pat]:
            if not Path(p).exists():
                continue
            for line in Path(p).read_text().splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prev = out.get(str(rec.get("npi")))
                if prev and prev.get("status") == "found" and rec.get("status") != "found":
                    rec = {**rec, **{k: v for k, v in prev.items() if k in ("status", "location", "network", "year")}}
                out[str(rec.get("npi"))] = {**(prev or {}), **rec}
    return out


def build_rows(con, directory=None, coverage: dict | None = None, npis: set | None = None) -> list[dict]:
    coverage = coverage or {}
    by_npi, by_tin = defaultdict(list), defaultdict(list)
    for r in (directory.rows if directory else []):
        if r.get("_side") != "PPO":
            continue
        if r.get("npi"):
            by_npi[str(r["npi"])].append(r)
        if r.get("tin"):
            by_tin[_digits(r["tin"])].append(r)
    rates = dict(((n, t), (k, c)) for n, t, k, c in con.execute(
        "SELECT p.npi, p.tin, COUNT(*), COUNT(DISTINCT s.code) FROM c4_pairs p JOIN c4_sig_rates s "
        "ON s.file_id = p.file_id AND s.sig_id = p.sig_id GROUP BY p.npi, p.tin"))
    pairs = defaultdict(lambda: {"refs": {}, "names": set()})
    cols = [r[1] for r in con.execute("PRAGMA table_info(c4_ref_members)")]
    name_col = "m.tin_name" if "tin_name" in cols else "''"
    for npi, tin, ref, st, basis, name in con.execute(
            f"SELECT m.npi, m.tin, m.ref_id, r.status, r.status_basis, {name_col} FROM c4_ref_members m "
            "JOIN c4_refs r ON r.file_id = m.file_id AND r.ref_id = m.ref_id WHERE m.npi IS NOT NULL ORDER BY 1, 2"):
        if npis is not None and str(npi) not in npis:
            continue
        p = pairs[(npi, tin)]
        p["refs"][ref] = (st, basis or "")
        if name:
            p["names"].add(name)
    out = []
    for (npi, tin), p in pairs.items():
        sn, tn = str(npi), _digits(tin)
        best = max(p["refs"].values(), key=lambda x: RANK.get(x[0], 0))
        cov = coverage.get(sn, {})
        own = by_npi.get(sn, [])
        own_tin = [r for r in own if not r.get("tin") or _digits(r["tin"]) == tn]
        same_tin = [r for r in by_tin.get(tn, []) if str(r.get("npi")) != sn]
        if own_tin:
            verdict = "FOUND_PPO_BY_NPI"
        elif own:
            verdict = "FOUND_PPO_NPI_OTHER_TIN"
        elif same_tin:
            verdict = "NOT_FOUND_BY_NPI_SAME_TIN"
        elif cov:
            verdict = "NOT_FOUND"
        else:
            verdict = "NOT_CHECKED"
        listed = own_tin or own
        k, c = rates.get((npi, tin), (0, 0))
        out.append({
            "npi": npi, "tin": tin, "tin_name": " | ".join(sorted(p["names"])),
            "rate_file_refs": " | ".join(sorted(p["refs"], key=lambda x: (len(x), x))), "ref_count": len(p["refs"]),
            "ref_statuses": " | ".join(f"{r}:{st}" for r, (st, _) in sorted(p["refs"].items(), key=lambda x: (len(x[0]), x[0]))),
            "pair_status": best[0], "status_basis": best[1], "rates_extracted": k, "codes_extracted": c,
            "site_checked": "yes" if cov else "no", "search_location": cov.get("location", ""),
            "network_searched": cov.get("network", ""), "network_year": cov.get("year", ""),
            "npi_search_result": cov.get("status", ""),
            "directory_name": " | ".join(sorted({r.get("name", "") for r in listed} - {""})),
            "directory_tin": " | ".join(sorted({_digits(r.get("tin")) for r in listed} - {""})),
            "directory_listing": " | ".join(sorted({r.get("listing_networks", "") for r in listed} - {""})),
            "directory_addresses": " | ".join(sorted({", ".join(x for x in (r.get("address_line1"), r.get("city"),
                                                                              r.get("zip")) if x) for r in listed} - {""})),
            "directory_specialty": " | ".join(sorted({r.get("specialty", "") for r in listed} - {""})),
            "tin_fallback_term": cov.get("fallback_term", ""),
            "tin_fallback_listings": len(same_tin) if (same_tin or cov.get("fallback_term")) else "",
            "tin_fallback_names": " | ".join(sorted({r.get("name", "") for r in same_tin} - {""})[:5]),
            "tin_fallback_npis": " | ".join(sorted({str(r.get("npi")) for r in same_tin if r.get("npi")})[:10]),
            "verdict": verdict,
        })
    return out


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


SITE_FOUND = ("FOUND_PPO_BY_NPI", "FOUND_PPO_NPI_OTHER_TIN")


def from_csv(path) -> tuple[list[dict], dict[str, dict]]:
    """Reuse an earlier c4_ppo_check.csv instead of searching the site again: the directory rows it
    implies (each listing it found, and the other NPIs listed under the same TIN) and, per NPI, what
    was searched (the same fields as a `ptd_dir check` coverage line)."""
    rows, coverage = [], {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            npi = str(r.get("npi") or "").strip()
            if not npi or r.get("site_checked") != "yes":
                continue
            net = r.get("network_searched", "")
            coverage.setdefault(npi, {"npi": npi, "tin": _digits(r.get("tin")), "status": r.get("npi_search_result", ""),
                                      "location": r.get("search_location", ""), "network": net,
                                      "year": r.get("network_year", ""), "fallback_term": r.get("tin_fallback_term", "")})
            base = {"network_name": net, "listing_networks": r.get("directory_listing", ""),
                    "npi_source": "listed", "source": f"ppo_check:{Path(path).name}"}
            if r.get("verdict") in SITE_FOUND:
                tins = [t for t in r.get("directory_tin", "").split(" | ") if t] or [""]
                addrs = [a for a in r.get("directory_addresses", "").split(" | ") if a] or [""]
                for tin in tins:
                    for a in addrs:
                        parts = a.rsplit(", ", 2)
                        line1, city, zip5 = (parts + ["", ""])[:3] if len(parts) == 3 else (a, "", "")
                        rows.append({**base, "npi": npi, "tin": tin, "name": r.get("directory_name", ""),
                                     "specialty": r.get("directory_specialty", ""), "address_line1": line1,
                                     "city": city, "state": "CA" if city else "", "zip": zip5})
            for other in [n for n in r.get("tin_fallback_npis", "").split(" | ") if n]:
                rows.append({**base, "npi": other, "tin": _digits(r.get("tin")), "listing_networks": "",
                             "name": "", "address_line1": "", "city": "", "state": "", "zip": ""})
    return rows, coverage
