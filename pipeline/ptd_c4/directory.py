"""Payer provider directory: which network each NPI is listed in, and at which address.

Two uses:
  C4  network evidence: an NPI/TIN listed under a PPO network (or only under HMO networks)
  C5  an address source that is independent of NPPES and specific to the payer's network

Input is a "directory table" (CSV/XLSX) with an `npi` column and a network column, plus optional
`tin` and address columns. `python -m ptd_c4 directory --fhir-base URL` builds that table from a
FHIR R4 provider directory that follows the Da Vinci PDex Plan-Net guide (Centene publishes one,
no login): networks are Organizations of type `ntwk`; PractitionerRole and OrganizationAffiliation
link NPIs to a network and to Locations.
"""
from __future__ import annotations

import csv
import re
import time
from collections import defaultdict
from pathlib import Path

import requests

from ptd_c2.network_probe import label_network_name

NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
TIN_SYSTEMS = ("urn:oid:2.16.840.1.113883.4.4", "http://terminology.hl7.org/NamingSystem/USEIN")
FIELDS = ["npi", "tin", "network_name", "network_label", "address_line1", "address_line2", "city", "state",
          "zip", "source", "last_updated"]
PPO, NON = "PPO", "NON_PPO"


def _key(h) -> str:
    return re.sub(r"[\s_\-]", "", str(h or "").lower())


def side_of(label: str, ppo_labels) -> str | None:
    label = str(label or "").upper()
    if not label or label == "UNKNOWN":
        return None
    if label in ("PPO", *ppo_labels):
        return PPO
    return NON


class Directory:
    enabled = True

    def __init__(self, rows: list[dict], c2cfg: dict, ppo_labels):
        self.rows = rows
        self.by_npi: dict[int, set] = defaultdict(set)
        self.by_tin: dict[str, set] = defaultdict(set)
        for r in rows:
            label = r.get("network_label") or label_network_name(str(r.get("network_name") or ""), c2cfg)
            side = side_of(label, ppo_labels)
            r["_side"] = side
            if side is None:
                continue
            if r.get("npi"):
                self.by_npi[int(r["npi"])].add(side)
            if r.get("tin"):
                self.by_tin[str(r["tin"])].add(side)

    def sides(self, npi, tin) -> set:
        s = set(self.by_npi.get(int(npi), ())) if npi is not None else set()
        if not s and tin:  # a TIN match only counts when the NPI itself is not listed
            s = set(self.by_tin.get(str(tin), ()))
        return s


def read_table(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl

        with open(path, "rb") as fh:
            ws = openpyxl.load_workbook(fh, read_only=True, data_only=True).worksheets[0]
            it = ws.iter_rows(values_only=True)
            headers = [str(h) for h in next(it)]
            recs = [dict(zip(headers, r)) for r in it]
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            recs = list(csv.DictReader(fh))
    return recs


def load_directory(path: str | Path, c2cfg: dict, ppo_labels) -> Directory:
    aliases = {
        "npi": ["npi"], "tin": ["tin", "ein", "taxid"],
        "network_name": ["networkname", "network", "plannetwork"], "network_label": ["networklabel", "networktype"],
        "address_line1": ["addressline1", "address1", "line1", "address", "street"],
        "address_line2": ["addressline2", "address2", "line2", "suite"],
        "city": ["city"], "state": ["state", "st"], "zip": ["zip", "zipcode", "postalcode"],
        "source": ["source"], "last_updated": ["lastupdated", "updated", "asof"],
    }
    rows = []
    for rec in read_table(path):
        cols = {_key(k): k for k in rec}
        r = {f: next((rec[cols[a]] for a in al if a in cols and rec[cols[a]] not in (None, "")), "")
             for f, al in aliases.items()}
        npi = re.sub(r"\D", "", str(r["npi"]).split(".")[0])
        r["npi"] = int(npi) if len(npi) == 10 else ""
        r["tin"] = re.sub(r"\D", "", str(r["tin"]).split(".")[0])
        r["source"] = r["source"] or f"directory:{Path(path).name}"
        if r["npi"] or r["tin"]:
            rows.append(r)
    if not rows:
        raise ValueError(f"directory table {path} has no rows with an npi or tin column")
    return Directory(rows, c2cfg, ppo_labels)


# ------------------------------------------------------------------ FHIR Plan-Net crawler
class FhirClient:
    def __init__(self, base: str, timeout: int = 60, retries: int = 3, max_pages: int = 1000,
                 user_agent: str = "hilabs-c4-pipeline/1.0", log=print):
        self.base, self.timeout, self.retries, self.max_pages, self.log = base.rstrip("/"), timeout, retries, max_pages, log
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/fhir+json", "User-Agent": user_agent})

    def _get(self, url: str, params=None) -> dict:
        last = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError:
                raise
            except requests.RequestException as exc:
                last = exc
                time.sleep(2 ** attempt)
        raise last

    def search(self, resource: str, params: dict):
        """Yield every entry resource of a search, following Bundle 'next' links."""
        url, p, pages = f"{self.base}/{resource}", dict(params), 0
        while url and pages < self.max_pages:
            bundle = self._get(url, p)
            pages += 1
            for e in bundle.get("entry") or []:
                if e.get("resource"):
                    yield e["resource"]
            url = next((ln.get("url") for ln in bundle.get("link") or [] if ln.get("relation") == "next"), None)
            p = None  # the next link carries its own query
        if url:
            self.log(f"[dir] stopped {resource} after {pages} pages (max_pages)")


def _ident(resource: dict, systems) -> str:
    for i in resource.get("identifier") or []:
        if i.get("system") in systems and i.get("value"):
            return re.sub(r"\D", "", str(i["value"]))
    return ""


def _address(loc: dict) -> dict:
    a = loc.get("address") or {}
    lines = a.get("line") or []
    return {"address_line1": lines[0] if lines else "", "address_line2": " ".join(lines[1:]),
            "city": a.get("city", ""), "state": a.get("state", ""), "zip": a.get("postalCode", "")}


def _ref_id(ref: dict | None) -> str:
    return str((ref or {}).get("reference") or "").split("/")[-1]


def crawl_plannet(base: str, out_csv: str | Path, c2cfg: dict, network_filter: str | None = None,
                  state: str | None = "CA", max_pages: int = 1000, log=print) -> int:
    """Crawl networks, then each network's practitioner roles and organisation affiliations."""
    client = FhirClient(base, max_pages=max_pages, log=log)
    nets = {}
    for org in client.search("Organization", {"type": "ntwk", "_count": 200}):
        name = org.get("name") or ""
        if network_filter and not re.search(network_filter, name, re.IGNORECASE):
            continue
        nets[org["id"]] = (name, label_network_name(name, c2cfg))
    log(f"[dir] {len(nets)} networks: " + "; ".join(f"{n} -> {lab}" for n, lab in list(nets.values())[:20]))
    rows = []
    for net_id, (net_name, net_label) in nets.items():
        for resource, incl_who, incl_loc, who_key, loc_key, org_key in (
            ("PractitionerRole", "PractitionerRole:practitioner", "PractitionerRole:location", "practitioner",
             "location", "organization"),
            ("OrganizationAffiliation", "OrganizationAffiliation:participating-organization",
             "OrganizationAffiliation:location", "participatingOrganization", "location", None),
        ):
            who, locs, roles = {}, {}, []
            params = {"network": f"Organization/{net_id}", "_include": [incl_who, incl_loc], "_count": 200}
            for res in client.search(resource, params):
                rt = res.get("resourceType")
                if rt == resource:
                    roles.append(res)
                elif rt == "Location":
                    locs[res["id"]] = res
                elif rt in ("Practitioner", "Organization"):
                    who[res["id"]] = res
            for role in roles:
                person = who.get(_ref_id(role.get(who_key)))
                npi = _ident(person, (NPI_SYSTEM,)) if person else ""
                tin = _ident(person, TIN_SYSTEMS) if person else ""
                if not npi:
                    continue
                updated = (role.get("meta") or {}).get("lastUpdated", "")[:10]
                for lref in role.get(loc_key) or [{}]:
                    loc = locs.get(_ref_id(lref))
                    addr = _address(loc) if loc else {}
                    if state and addr and str(addr.get("state", "")).upper() not in ("", state.upper()):
                        continue
                    rows.append({"npi": npi, "tin": tin, "network_name": net_name, "network_label": net_label,
                                 **addr, "source": f"fhir:{resource}", "last_updated": updated})
        log(f"[dir]   {net_name}: {len(rows):,} directory rows so far")
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)
