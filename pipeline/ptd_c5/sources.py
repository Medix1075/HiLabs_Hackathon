"""Readers for every address source. Each yields candidate addresses for the NPIs C4 extracted.

Every reader streams its file once and keeps only the NPIs asked for, so the 9 GB NPPES file
needs a few hundred MB of memory at most. Column names are matched loosely (case, spaces and
punctuation ignored, with aliases), so the monthly NPPES file, an organiser-made CA extract and
older or newer CMS layouts all load.

  nppes      NPPES main file: practice location, mailing address, entity type, taxonomies, dates
  nppes_pl   NPPES Practice Location Reference File: secondary practice locations
  dac        CMS Doctors and Clinicians file (Medicare PECOS): NPI x group (org_pac_id) x address
  directory  payer directory table (see ptd_c4/directory.py), e.g. from Centene's FHIR API
  npi_api    NPI Registry API, for NPIs missing from the files (capped number of lookups)
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import requests

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))


def _key(h) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


NPPES_COLS = {
    "npi": ["npi"],
    "entity_type": ["entitytypecode", "entitytype", "npitype"],
    "org_name": ["providerorganizationnamelegalbusinessname", "organizationname", "orgname"],
    "last_name": ["providerlastnamelegalname", "lastname"],
    "first_name": ["providerfirstname", "firstname"],
    "credential": ["providercredentialtext", "credential"],
    "p_line1": ["providerfirstlinebusinesspracticelocationaddress", "practiceaddressline1", "practiceaddress1"],
    "p_line2": ["providersecondlinebusinesspracticelocationaddress", "practiceaddressline2", "practiceaddress2"],
    "p_city": ["providerbusinesspracticelocationaddresscityname", "practicecity"],
    "p_state": ["providerbusinesspracticelocationaddressstatename", "practicestate"],
    "p_zip": ["providerbusinesspracticelocationaddresspostalcode", "practicezip", "practicepostalcode"],
    "m_line1": ["providerfirstlinebusinessmailingaddress", "mailingaddressline1", "mailingaddress1"],
    "m_line2": ["providersecondlinebusinessmailingaddress", "mailingaddressline2", "mailingaddress2"],
    "m_city": ["providerbusinessmailingaddresscityname", "mailingcity"],
    "m_state": ["providerbusinessmailingaddressstatename", "mailingstate"],
    "m_zip": ["providerbusinessmailingaddresspostalcode", "mailingzip", "mailingpostalcode"],
    "last_update": ["lastupdatedate", "lastupdated"],
    "enumeration_date": ["providerenumerationdate", "enumerationdate"],
    "deactivation_date": ["npideactivationdate", "deactivationdate"],
    "reactivation_date": ["npireactivationdate", "reactivationdate"],
}
PL_COLS = {
    "npi": ["npi"],
    "line1": ["providersecondarypracticelocationaddressaddressline1", "addressline1", "address1"],
    "line2": ["providersecondarypracticelocationaddressaddressline2", "addressline2", "address2"],
    "city": ["providersecondarypracticelocationaddresscityname", "city", "cityname"],
    "state": ["providersecondarypracticelocationaddressstatename", "state", "statename"],
    "zip": ["providersecondarypracticelocationaddresspostalcode", "zip", "postalcode"],
}
DAC_COLS = {
    "npi": ["npi"],
    "pac": ["orgpacid"],
    "org_name": ["facilityname", "orgnm"],
    "org_members": ["numorgmem"],
    "pri_spec": ["prispec"],
    "line1": ["adrln1"],
    "line2": ["adrln2"],
    "line2_suppressed": ["ln2sprs"],
    "city": ["citytown", "cty", "city"],
    "state": ["state", "st"],
    "zip": ["zipcode", "zip"],
    "address_id": ["adrsid"],
    "last_name": ["providerlastname", "lstnm"],
    "first_name": ["providerfirstname", "frstnm"],
}


def resolve_columns(headers: list[str], spec: dict) -> dict[str, int]:
    keys = [_key(h) for h in headers]
    out = {}
    for field, aliases in spec.items():
        for a in aliases:
            if a in keys:
                out[field] = keys.index(a)
                break
    return out


def _taxonomy_columns(headers: list[str]) -> list[tuple[int, int | None]]:
    keys = [_key(h) for h in headers]
    cols = []
    for i, k in enumerate(keys):
        m = re.fullmatch(r"healthcareprovidertaxonomycode(\d+)", k) or re.fullmatch(r"taxonomycode(\d+)", k)
        if m:
            n = m.group(1)
            sw = next((j for j, k2 in enumerate(keys)
                       if k2 in (f"healthcareproviderprimarytaxonomyswitch{n}", f"primarytaxonomyswitch{n}")), None)
            cols.append((i, sw))
    if not cols and "primarytaxonomy" in keys:
        cols.append((keys.index("primarytaxonomy"), None))
    return cols


def open_table(path: str | Path, member_rx: str | None = None):
    """Yield (headers, row iterator) for a CSV, a CSV inside a ZIP, or the first sheet of an XLSX."""
    path = Path(path)
    if zipfile.is_zipfile(path) and path.suffix.lower() not in (".xlsx", ".xlsm"):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith(".csv") and "fileheader" not in n.lower()]
        if member_rx:
            names = [n for n in names if re.search(member_rx, Path(n).name, re.IGNORECASE)]
        if not names:
            raise ValueError(f"{path}: no CSV member matching {member_rx!r}")
        fh = io.TextIOWrapper(zf.open(sorted(names, key=len)[0]), encoding="utf-8", errors="replace", newline="")
        reader = csv.reader(fh)
        return next(reader), reader
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        headers = [str(h or "") for h in next(it)]
        return headers, ([("" if v is None else str(v)) for v in r] for r in it)
    fh = open(path, newline="", encoding="utf-8-sig", errors="replace")
    reader = csv.reader(fh)
    return next(reader), reader


def parse_date(s) -> date | None:
    s = str(s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _npi(v) -> int | None:
    s = re.sub(r"\D", "", str(v or "").split(".")[0])
    return int(s) if len(s) == 10 else None


def _get(row, cols, field) -> str:
    i = cols.get(field)
    return row[i].strip() if i is not None and i < len(row) and row[i] else ""


# ------------------------------------------------------------------ NPPES main file
def read_nppes(path, npis: set[int], log=print) -> tuple[dict, list[dict]]:
    """Returns ({npi: provider facts}, [candidate addresses])."""
    headers, rows = open_table(path, r"npidata_pfile|npidata|nppes")
    cols = resolve_columns(headers, NPPES_COLS)
    tax_cols = _taxonomy_columns(headers)
    if "npi" not in cols:
        raise ValueError(f"{path}: no NPI column")
    want = {str(n) for n in npis}
    ni = cols["npi"]
    providers, cands, scanned = {}, [], 0
    t0 = time.time()
    for row in rows:
        scanned += 1
        if ni >= len(row) or row[ni] not in want:
            continue
        npi = int(row[ni])
        taxonomies, primary = [], ""
        for ti, si in tax_cols:
            code = row[ti].strip() if ti < len(row) else ""
            if code:
                taxonomies.append(code)
                if si is not None and si < len(row) and row[si].strip().upper() == "Y":
                    primary = code
        updated = parse_date(_get(row, cols, "last_update"))
        deact, react = parse_date(_get(row, cols, "deactivation_date")), parse_date(_get(row, cols, "reactivation_date"))
        name = _get(row, cols, "org_name") or " ".join(
            x for x in (_get(row, cols, "first_name"), _get(row, cols, "last_name"), _get(row, cols, "credential")) if x)
        providers[npi] = {
            "npi": npi, "entity_type": _get(row, cols, "entity_type"), "name": name,
            "primary_taxonomy": primary or (taxonomies[0] if taxonomies else ""), "taxonomies": "|".join(taxonomies),
            "nppes_last_update": updated.isoformat() if updated else "",
            "deactivated": bool(deact and (not react or react < deact)), "source": "nppes",
        }
        for kind, pre in (("nppes_practice", "p_"), ("nppes_mailing", "m_")):
            if _get(row, cols, pre + "line1"):
                cands.append({"npi": npi, "source": kind, "line1": _get(row, cols, pre + "line1"),
                              "line2": _get(row, cols, pre + "line2"), "city": _get(row, cols, pre + "city"),
                              "state": _get(row, cols, pre + "state"), "zip": _get(row, cols, pre + "zip"),
                              "date": updated, "detail": ""})
    log(f"[c5]   NPPES: scanned {scanned:,} rows in {time.time() - t0:.0f}s, found {len(providers):,} of "
        f"{len(npis):,} NPIs")
    return providers, cands


def read_nppes_pl(path, npis: set[int], dates: dict[int, date | None], log=print) -> list[dict]:
    headers, rows = open_table(path, r"pl_pfile|practice.?location")
    cols = resolve_columns(headers, PL_COLS)
    want = {str(n) for n in npis}
    ni = cols.get("npi", 0)
    cands = []
    for row in rows:
        if ni >= len(row) or row[ni] not in want:
            continue
        npi = int(row[ni])
        cands.append({"npi": npi, "source": "nppes_secondary", "line1": _get(row, cols, "line1"),
                      "line2": _get(row, cols, "line2"), "city": _get(row, cols, "city"),
                      "state": _get(row, cols, "state"), "zip": _get(row, cols, "zip"),
                      "date": dates.get(npi), "detail": ""})
    log(f"[c5]   NPPES practice locations: {len(cands):,} secondary addresses")
    return cands


# ------------------------------------------------------------------ CMS Doctors and Clinicians
def read_dac(path, npis: set[int], as_of: date, log=print) -> tuple[dict, list[dict]]:
    headers, rows = open_table(path, r"dac|national|doctors")
    cols = resolve_columns(headers, DAC_COLS)
    want = {str(n) for n in npis}
    ni = cols.get("npi", 0)
    facts, cands = {}, []
    for row in rows:
        if ni >= len(row) or row[ni].strip() not in want:
            continue
        npi = int(row[ni].strip())
        f = facts.setdefault(npi, {"pri_spec": _get(row, cols, "pri_spec"), "pacs": set(),
                                   "name": f"{_get(row, cols, 'first_name')} {_get(row, cols, 'last_name')}".strip()})
        pac = _get(row, cols, "pac")
        if pac:
            f["pacs"].add(pac)
        line2 = "" if _get(row, cols, "line2_suppressed").upper() == "Y" else _get(row, cols, "line2")
        cands.append({"npi": npi, "source": "dac", "line1": _get(row, cols, "line1"), "line2": line2,
                      "city": _get(row, cols, "city"), "state": _get(row, cols, "state"),
                      "zip": _get(row, cols, "zip"), "date": as_of,
                      "detail": json.dumps({"org_pac_id": pac, "org_name": _get(row, cols, "org_name"),
                                            "address_id": _get(row, cols, "address_id")})})
    log(f"[c5]   Doctors and Clinicians: {len(facts):,} NPIs, {len(cands):,} NPI x group x address rows")
    return facts, cands


# ------------------------------------------------------------------ payer directory
def directory_candidates(directory, npis: set[int], as_of: date) -> list[dict]:
    cands = []
    for r in directory.rows:
        if r.get("npi") in npis and r.get("address_line1"):
            cands.append({"npi": r["npi"], "source": "directory", "line1": r["address_line1"],
                          "line2": r.get("address_line2", ""), "city": r.get("city", ""), "state": r.get("state", ""),
                          "zip": r.get("zip", ""), "date": parse_date(r.get("last_updated")) or as_of,
                          "detail": json.dumps({"tin": r.get("tin", ""), "network": r.get("network_name", ""),
                                                "side": r.get("_side")})})
    return cands


# ------------------------------------------------------------------ NPI Registry API
def npi_api_lookup(npis: list[int], cfg: dict, cache_path: Path, log=print) -> tuple[dict, list[dict]]:
    ac = cfg["npi_api"]
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text().splitlines():
            rec = json.loads(line)
            cache[rec["npi"]] = rec["result"]
    todo = [n for n in npis if n not in cache][: ac["max_lookups"]]
    fails = 0
    with open(cache_path, "a") as fh:
        for n in todo:
            try:
                resp = requests.get(ac["base_url"], params={"version": "2.1", "number": n},
                                    timeout=ac["timeout_seconds"], headers={"User-Agent": ac["user_agent"]})
                resp.raise_for_status()
                result = (resp.json().get("results") or [None])[0]
            except (requests.RequestException, ValueError) as exc:
                fails += 1
                if fails >= ac["stop_after_failures"]:
                    log(f"[c5]   NPI API: stopping after {fails} failures ({exc.__class__.__name__}: {exc})")
                    break
                continue
            cache[n] = result
            fh.write(json.dumps({"npi": n, "result": result}) + "\n")
            time.sleep(1.0 / ac["requests_per_second"])
    providers, cands = {}, []
    for n in npis:
        res = cache.get(n)
        if not res:
            continue
        basic = res.get("basic") or {}
        updated = parse_date(basic.get("last_updated"))
        taxes = res.get("taxonomies") or []
        primary = next((t.get("code") for t in taxes if t.get("primary")), taxes[0].get("code") if taxes else "")
        providers[n] = {
            "npi": n, "entity_type": "1" if res.get("enumeration_type") == "NPI-1" else "2",
            "name": basic.get("organization_name") or f"{basic.get('first_name', '')} {basic.get('last_name', '')}".strip(),
            "primary_taxonomy": primary or "", "taxonomies": "|".join(t.get("code", "") for t in taxes),
            "nppes_last_update": updated.isoformat() if updated else "",
            "deactivated": str(basic.get("status", "A")).upper() == "D", "source": "npi_api",
        }
        for a in (res.get("addresses") or []):
            kind = "npi_api_practice" if a.get("address_purpose") == "LOCATION" else "nppes_mailing"
            cands.append({"npi": n, "source": kind, "line1": a.get("address_1", ""), "line2": a.get("address_2", ""),
                          "city": a.get("city", ""), "state": a.get("state", ""), "zip": a.get("postal_code", ""),
                          "date": updated, "detail": "api"})
        for a in (res.get("practiceLocations") or []):
            cands.append({"npi": n, "source": "npi_api_secondary", "line1": a.get("address_1", ""),
                          "line2": a.get("address_2", ""), "city": a.get("city", ""), "state": a.get("state", ""),
                          "zip": a.get("postal_code", ""), "date": updated, "detail": "api"})
    log(f"[c5]   NPI Registry API: {len(todo):,} looked up ({fails} failed), {len(providers):,} NPIs known")
    return providers, cands
