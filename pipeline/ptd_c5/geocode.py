"""Merge sites that are one place spelled two ways, with the US Census Bureau geocoder.

The local standardiser (address.py) keys a site by house number + street + ZIP5, so "4300 Long Beach
Blvd" and "4300 N Long Beach Boulevard" can end up as two sites when the sources disagree on a
directional, an abbreviation it doesn't know or a misspelling. The Census geocoder
(https://geocoding.geo.census.gov, free, no key) matches each address to its street segment and
returns one canonical spelling. Sites whose canonical address is the same, with the same house
number, become one site; the alias keeps the first site key of the group, so sites that merge with
nothing keep their key and nothing else changes.

Addresses go in batches (the batch endpoint takes up to 10,000), and every answer, including "no
match", is cached in a JSONL file so a rerun only asks for new addresses. If the service can't be
reached the run carries on with the local keys.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests

from .address import Address


def _query(a: Address) -> tuple[str, str, str, str]:
    return a.line1, a.city, a.state, a.zip5


def _qkey(q: tuple) -> str:
    return "|".join(q)


def _house(line1: str) -> str:
    m = re.match(r"^(\d+[A-Z]?)\b", line1 or "")
    return m.group(1) if m else ""


def read_cache(path: Path, days: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    cutoff = date.today().toordinal() - days
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if date.fromisoformat(rec.get("fetched") or "1970-01-01").toordinal() >= cutoff:
            out[rec["q"]] = rec
    return out


def parse_batch(text: str) -> dict[str, dict]:
    """The batch answer: id, input, Match | No_Match | Tie, Exact | Non_Exact, matched address, "lon,lat", ..."""
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            continue
        rid, status = row[0], row[2].strip()
        if status == "Match" and len(row) >= 6:
            lon, lat = (row[5].split(",") + [""])[:2]
            out[rid] = {"matched": row[4].strip().upper(), "exact": row[3].strip() == "Exact",
                        "lon": float(lon) if lon else None, "lat": float(lat) if lat else None}
        else:
            out[rid] = {"matched": "", "exact": False, "lon": None, "lat": None}
    return out


def geocode_batch(queries: list[tuple], gc: dict, session: requests.Session) -> dict[str, dict]:
    buf = io.StringIO()
    w = csv.writer(buf)
    for i, q in enumerate(queries):
        w.writerow([i, *q])
    resp = session.post(gc["batch_url"], data={"benchmark": gc.get("benchmark", "Public_AR_Current")},
                        files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
                        timeout=gc.get("timeout_seconds", 300))
    resp.raise_for_status()
    by_id = parse_batch(resp.text)
    return {_qkey(q): by_id.get(str(i), {"matched": "", "exact": False, "lon": None, "lat": None})
            for i, q in enumerate(queries)}


def site_aliases(addresses: dict[str, Address], cfg: dict, log=print, fetch=None) -> tuple[dict[str, str], dict]:
    """{site_key: canonical site_key} for sites that are the same place, and a small report.

    `addresses`: one standardised address per site key. `fetch(queries) -> {qkey: answer}` replaces
    the web call (tests)."""
    gc = cfg.get("geocode") or {}
    report = {"mode": gc.get("mode", "off"), "sites": len(addresses), "asked": 0, "cached": 0, "matched": 0,
              "merged_sites": 0, "error": ""}
    if gc.get("mode", "off") == "off":
        return {}, report
    sites = {k: a for k, a in addresses.items()
             if a.line1 and _house(a.line1) and not a.po_box and a.zip5
             and (not gc.get("state") or a.state in ("", gc["state"]))}
    cache_path = Path(gc.get("cache_path", "data/geocode_cache.jsonl"))
    cache = read_cache(cache_path, gc.get("cache_days", 365))
    todo = sorted({_qkey(_query(a)): _query(a) for a in sites.values() if _qkey(_query(a)) not in cache}.values())
    todo = todo[: gc.get("max_lookups", 100000)]
    report["cached"] = len(sites) - len(todo)
    if todo:
        log(f"[c5]   Census geocoder: {len(todo):,} addresses to match ({report['cached']:,} cached)")
        session = requests.Session()
        session.headers["User-Agent"] = gc.get("user_agent", "hilabs-c5-pipeline/1.0")
        size = gc.get("batch_size", 1000)
        today = date.today().isoformat()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        for i in range(0, len(todo), size):
            chunk = todo[i:i + size]
            try:
                got = fetch(chunk) if fetch else geocode_batch(chunk, gc, session)
            except (requests.RequestException, ValueError) as exc:
                report["error"] = f"{exc.__class__.__name__}: {exc}"[:300]
                log(f"[c5]   Census geocoder unavailable ({report['error']}); sites keep their local keys")
                break
            with open(cache_path, "a", encoding="utf-8") as fh:
                for k, ans in got.items():
                    rec = {"q": k, **ans, "fetched": today}
                    cache[k] = rec
                    fh.write(json.dumps(rec) + "\n")
            report["asked"] += len(chunk)
    groups: dict[str, list] = defaultdict(list)
    for key, a in sites.items():
        ans = cache.get(_qkey(_query(a)))
        if not ans or not ans.get("matched"):
            continue
        report["matched"] += 1
        street, _, rest = ans["matched"].partition(",")
        zip5 = re.sub(r"\D", "", rest.rsplit(",", 1)[-1])[:5] or a.zip5
        if _house(street) != _house(a.line1):  # matched to another number on the segment: not the same building
            continue
        groups[f"{street.strip()}|{zip5}"].append(key)
    alias = {}
    for keys in groups.values():
        if len(keys) > 1:
            canon = min(keys)
            for k in keys:
                if k != canon:
                    alias[k] = canon
    report["merged_sites"] = len(alias)
    log(f"[c5]   Census geocoder: {report['matched']:,} of {len(sites):,} sites matched; "
        f"{len(alias):,} sites merged into another spelling of the same address")
    return alias, report
