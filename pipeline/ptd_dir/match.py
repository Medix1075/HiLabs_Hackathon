"""Give an NPI to directory listings that show none, and prepare search targets.

Consumer directories often show a name and an address but no NPI. A listing is linked to an NPI
only when the link is unambiguous:

  name+address  first and last name match a provider of this pipeline (C5's providers) or of the NPI
                Registry, AND the street address is the same site (house number + street + ZIP).
                Two independent identifiers: treated like a listed NPI.
  name+zip      the name matches exactly one provider in that ZIP, but the street differs or is
                missing: C4 counts it as weak evidence (a PPO candidate, never a confirmation).

A name that matches more than one provider is left without an NPI.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict

import requests

from ptd_c4.db import table_exists
from ptd_c5.address import standardize

TITLES = {"MD", "DO", "DR", "NP", "PA", "PAC", "FNP", "DPM", "DDS", "DMD", "OD", "PHD", "PSYD", "LCSW", "MFT",
          "LMFT", "RN", "CNM", "JR", "SR", "II", "III", "IV", "MBBS", "FACP", "FACC", "FAAP", "CRNA", "DC", "AUD"}


def name_key(first: str, last: str, full: str = "") -> tuple[str, str] | None:
    """(FIRST, LAST) in upper case, without titles, middle names or punctuation."""
    if not (first and last) and full:
        toks = [t for t in re.sub(r"[^A-Za-z0-9\s,\-']", " ", full).replace(",", " , ").split()]
        if "," in toks:  # "LAST, FIRST M"
            i = toks.index(",")
            last, first = " ".join(toks[:i]), " ".join(toks[i + 1:])
        else:
            words = [t for t in toks if t.upper().strip(".") not in TITLES]
            if len(words) < 2:
                return None
            first, last = words[0], words[-1]
    f = [t for t in re.sub(r"[^A-Za-z0-9\s\-']", " ", first or "").upper().split() if t not in TITLES]
    l = [t for t in re.sub(r"[^A-Za-z0-9\s\-']", " ", last or "").upper().split() if t not in TITLES]
    if not f or not l:
        return None
    return f[0], l[-1]


class ProviderIndex:
    """(FIRST, LAST) -> [(npi, zip5, site_key)] from the pipeline's rate store (C5 providers + addresses)."""

    def __init__(self, db: str | None):
        self.by_name: dict[tuple, list] = defaultdict(list)
        if not db:
            return
        con = sqlite3.connect(db)
        if not (table_exists(con, "c5_providers") and table_exists(con, "c5_addresses")):
            con.close()
            return
        names = {npi: name for npi, name, et in con.execute("SELECT npi, name, entity_type FROM c5_providers")
                 if str(et) != "2"}
        for npi, z, site in con.execute("SELECT DISTINCT npi, zip5, site_key FROM c5_addresses"):
            k = name_key("", "", names.get(npi, ""))
            if k:
                self.by_name[k].append((npi, z or "", site or ""))
        con.close()

    def match(self, key, zip5: str, site: str) -> tuple[str, str]:
        cands = self.by_name.get(key, [])
        same_site = {n for n, z, s in cands if site and s == site}
        if len(same_site) == 1:
            return str(next(iter(same_site))), "name+address"
        same_zip = {n for n, z, s in cands if zip5 and z == zip5}
        if len(same_zip) == 1:
            return str(next(iter(same_zip))), "name+zip"
        return "", ""


def npi_registry_search(first: str, last: str, state: str, zip5: str, base: str, session: requests.Session,
                        timeout: int = 20) -> list[dict]:
    params = {"version": "2.1", "first_name": first, "last_name": last, "state": state or "CA", "limit": 50}
    if zip5:
        params["postal_code"] = zip5
    r = session.get(base, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json().get("results") or []


def assign_npis(rows: list[dict], db: str | None, use_api: bool = False,
                api_base: str = "https://npiregistry.cms.hhs.gov/api/", rps: float = 4.0, log=print) -> dict:
    idx = ProviderIndex(db)
    session = requests.Session()
    stats = defaultdict(int)
    api_cache: dict[tuple, list] = {}
    for r in rows:
        if r.get("npi"):
            stats["listed"] += 1
            continue
        key = name_key(r.get("first_name", ""), r.get("last_name", ""), r.get("name", ""))
        if not key:
            stats["no_person_name"] += 1
            continue
        a = standardize(r.get("address_line1", ""), r.get("address_line2", ""), r.get("city", ""),
                        r.get("state", ""), r.get("zip", ""))
        npi, how = idx.match(key, a.zip5, a.site_key if r.get("address_line1") else "")
        if not npi and use_api:
            ck = (key, a.zip5)
            if ck not in api_cache:
                try:
                    api_cache[ck] = npi_registry_search(key[0], key[1], r.get("state", "") or "CA", a.zip5, api_base, session)
                except Exception as exc:
                    stats["api_errors"] += 1
                    api_cache[ck] = []
                    if stats["api_errors"] <= 3:
                        log(f"[dir] NPI Registry search failed: {exc.__class__.__name__}")
                time.sleep(1.0 / rps)
            same_site, same_zip = set(), set()
            for res in api_cache[ck]:
                for ad in res.get("addresses") or []:
                    if ad.get("address_purpose") != "LOCATION":
                        continue
                    b = standardize(ad.get("address_1", ""), ad.get("address_2", ""), ad.get("city", ""),
                                    ad.get("state", ""), ad.get("postal_code", ""))
                    if r.get("address_line1") and b.site_key == a.site_key:
                        same_site.add(str(res.get("number")))
                    if b.zip5 == a.zip5:
                        same_zip.add(str(res.get("number")))
            if len(same_site) == 1:
                npi, how = next(iter(same_site)), "name+address"
            elif len(same_zip) == 1:
                npi, how = next(iter(same_zip)), "name+zip"
        if npi:
            r["npi"], r["npi_source"] = npi, how
            stats[how] += 1
        else:
            stats["unmatched"] += 1
    return dict(stats)


def search_targets(db: str, what: str) -> list[str]:
    """Values to vary in a replay: the ZIPs, NPIs or names of C4's providers that are not yet confirmed."""
    con = sqlite3.connect(db)
    if what == "zip":
        q = ("SELECT DISTINCT a.zip5 FROM c5_addresses a JOIN c6_rates r ON r.npi = a.npi AND r.tin = a.tin "
             "WHERE a.zip5 != '' ORDER BY 1")
    elif what == "npi":
        q = "SELECT DISTINCT npi FROM c6_rates WHERE network_status != 'CONFIRMED_PPO' ORDER BY 1"
    elif what == "name":
        q = ("SELECT DISTINCT p.name FROM c5_providers p JOIN c6_rates r ON r.npi = p.npi "
             "WHERE r.network_status != 'CONFIRMED_PPO' AND p.name != '' ORDER BY 1")
    else:
        raise ValueError(what)
    vals = [str(v) for (v,) in con.execute(q)]
    con.close()
    return vals
