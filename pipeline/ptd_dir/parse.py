"""Find provider listings in captured JSON, whatever shape the directory's API uses.

Directory APIs differ in field names but not in content: a provider has a name, usually an NPI, a
specialty and one or more locations with an address. So instead of hard-coding one site's schema,
the parser walks the JSON and recognises those parts by their key names:

  provider   a dict with an NPI-like key (npi, providerNpi, npiNumber ...) holding a valid NPI, or a
             name plus an address (on itself or on a child location)
  location   a dict with a street line, or a city and a ZIP
  networks   values under keys containing network / plan / product (strings, lists, or {name})

One row is written per provider x location. Rows keep the network chosen during the search
(`network_name`, from the capture) and the networks the listing itself names (`listing_networks`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from ptd_c3.scan import npi_is_valid

ROW_FIELDS = ["npi", "tin", "name", "first_name", "last_name", "specialty", "address_line1", "address_line2", "city",
              "state", "zip", "listing_networks", "network_name", "npi_source", "source", "last_updated", "request_url"]


def _k(key) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _scalar(v) -> str:
    if isinstance(v, (str, int, float)) and not isinstance(v, bool):
        return str(v).strip()
    return ""


PERSONISH = ("name", "provider", "practitioner", "doctor", "physician", "person", "demographic", "profile")


def _identity_children(d: dict) -> list[dict]:
    """Child objects that describe the provider itself ({"provider": {...}}, {"name": {...}})."""
    return [v for k, v in d.items() if isinstance(v, dict) and any(w in _k(k) for w in PERSONISH)]


def _npi(d: dict, deep: bool = True) -> str:
    for key, v in d.items():
        kk = _k(key)
        if "npi" not in kk:
            continue
        vals = v if isinstance(v, list) else [v]
        for x in vals:
            n = re.sub(r"\D", "", _scalar(x))
            if len(n) == 10 and npi_is_valid(int(n)):
                return n
    if deep:
        for c in _identity_children(d):
            n = _npi(c, deep=False)
            if n:
                return n
    return ""


def _first(d: dict, names: tuple) -> str:
    for key, v in d.items():
        if _k(key) in names:
            s = _scalar(v)
            if s:
                return s
    return ""


def _name(d: dict) -> tuple[str, str, str]:
    first = _first(d, ("firstname", "givenname", "first"))
    last = _first(d, ("lastname", "familyname", "surname", "last"))
    full = _first(d, ("providername", "fullname", "displayname", "practitionername", "organizationname",
                      "facilityname", "groupname", "name"))
    if not full and (first or last):
        full = f"{first} {last}".strip()
    for c in _identity_children(d):  # name in a child object: {"name": {first, last}}, {"provider": {...}}
        if full:
            break
        n2, f2, l2 = _name(c)
        first, last, full = first or f2, last or l2, n2
    return full, first, last


ADDR = {"line1": ("addressline1", "address1", "line1", "street", "streetaddress", "street1", "addressstreet1"),
        "line2": ("addressline2", "address2", "line2", "suite", "street2", "addressstreet2"),
        "city": ("city", "cityname"), "state": ("state", "statecode", "stateabbreviation"),
        "zip": ("zip", "zipcode", "postalcode", "zip5", "postal")}


def _address(d: dict) -> dict | None:
    a = {f: _first(d, names) for f, names in ADDR.items()}
    if not a["line1"]:
        line = _first(d, ("address", "addressline", "fulladdress"))
        if line and re.match(r"\d", line):
            a["line1"] = line
    lines = d.get("line") or d.get("lines")
    if not a["line1"] and isinstance(lines, list) and lines:
        a["line1"], a["line2"] = _scalar(lines[0]), " ".join(_scalar(x) for x in lines[1:])
    a["zip"] = re.sub(r"[^\d]", "", a["zip"])[:5]
    if a["line1"] or (a["city"] and a["zip"]):
        return a
    return None


def _collect(d, want, depth: int = 2) -> list[str]:
    """String values under keys whose name contains one of `want`, down to `depth` levels."""
    out = []
    if depth < 0 or not isinstance(d, dict):
        return out
    for key, v in d.items():
        hit = any(w in _k(key) for w in want)
        vals = v if isinstance(v, list) else [v]
        for x in vals:
            if hit and _scalar(x) and not re.fullmatch(r"[\d\-]+", _scalar(x)):
                out.append(_scalar(x))
            elif isinstance(x, dict):
                if hit:
                    nm = _first(x, ("name", "networkname", "planname", "productname", "description", "displayname"))
                    if nm:
                        out.append(nm)
                out += _collect(x, want, depth - 1)
    return out


def _is_provider(d: dict) -> bool:
    if _npi(d):
        return True
    full, _, _ = _name(d)
    return bool(full) and (_address(d) is not None or any(_locations_in(v) for v in d.values()))


def _locations_in(v) -> list[dict]:
    vals = v if isinstance(v, list) else [v]
    out = []
    for x in vals:
        if isinstance(x, dict) and not _npi(x):
            a = _address(x)
            if a:
                out.append(a)
            else:
                for y in x.values():  # e.g. {"location": {"address": {...}}}
                    if isinstance(y, dict):
                        a2 = _address(y)
                        if a2:
                            out.append(a2)
    return out


def providers_in(obj) -> list[dict]:
    rows: list[dict] = []

    def walk(o, depth=0, parent_npi=None, parent_locs=()):
        if depth > 40:
            return
        if isinstance(o, list):
            for x in o:
                walk(x, depth + 1, parent_npi, parent_locs)
            return
        if not isinstance(o, dict):
            return
        # inside a provider, only other NPI-bearing providers count (a group's practitioners), not its
        # locations or the child object that holds the provider's own name and NPI
        found = _is_provider(o) and (parent_npi is None or (bool(_npi(o)) and _npi(o) != parent_npi))
        if found:
            full, first, last = _name(o)
            locs = [a for a in [_address(o)] if a] + [a for v in o.values() for a in _locations_in(v)]
            locs = locs or list(parent_locs)  # a group's practitioner without its own address: the group's
            nets = sorted(set(_collect(o, ("network", "plan", "product"))))
            specs = sorted(set(_collect(o, ("specialt",))))
            base = {"npi": _npi(o), "tin": re.sub(r"\D", "", _first(o, ("tin", "taxid", "ein", "federaltaxid"))),
                    "name": full, "first_name": first, "last_name": last, "specialty": "|".join(specs[:5]),
                    "listing_networks": "|".join(nets[:20])}
            for a in locs or [{}]:
                rows.append({**base, "address_line1": a.get("line1", ""), "address_line2": a.get("line2", ""),
                             "city": a.get("city", ""), "state": a.get("state", ""), "zip": a.get("zip", "")})
        for v in o.values():  # nested providers (a group's practitioners) are found too
            if isinstance(v, (list, dict)):
                walk(v, depth + 1, (_npi(o) or "-") if found else parent_npi, locs if found else parent_locs)

    walk(obj)
    return rows


def read_capture(path: Path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def parse_captures(paths: list[Path], network_override: str | None = None) -> list[dict]:
    """Every provider x location in the captured responses, de-duplicated."""
    seen, out = set(), []
    for p in paths:
        for rec in read_capture(p):
            if int(rec.get("status") or 200) >= 400:
                continue
            for r in providers_in(rec.get("body")):
                r.update(network_name=network_override or rec.get("network_name", ""),
                         npi_source="listed" if r["npi"] else "", source=urlparse(rec.get("url", "")).netloc,
                         last_updated=str(rec.get("t", ""))[:10], request_url=rec.get("url", ""))
                key = (r["npi"], r["name"].upper(), r["address_line1"].upper(), r["zip"], r["network_name"])
                if key not in seen:
                    seen.add(key)
                    out.append(r)
    return out
