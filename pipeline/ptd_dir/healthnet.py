"""Health Net California's own Find a Provider site: check NPIs against a chosen network.

https://findaprovider.healthnetcalifornia.com is a JavaScript app (no login). The flow a person uses:
location (a California city) -> network year -> network (e.g. "PPO - Large Group/Small Group ...")
-> type the NPI in the search box. `check` drives that flow with Playwright for each NPI, using the
provider's own city from the NPI Registry, and records the site's search responses
(external-api.search.my.centene.com/pces/query) in the same JSONL format as `capture`.

Each result is a "carrierEntity": NPIs (nationalProviderIdentifiers), TIN (taxIdNumber), locations
with address and specialties, and the network it is listed in, e.g.
  network.product = {code: PPO, name: "HealthNet Commercial PPO", networkId: 8201}
  network.carrier = {code: PP,  name: "CA PPO Large and Small"}
  network.networkCycles = [{status: Par | Nonpar, startDate, endDate}]
`parse_pces` turns those into directory rows; a listing counts only when a Par cycle covers the year.

Search results are limited to 50 miles around the chosen city, and the site only finds the NPI it
stores: a facility that bills under another NPI is found by name/TIN only (C4 counts a TIN-only
match as weak). Not finding an NPI is therefore never used as evidence against PPO.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SITE = "https://findaprovider.healthnetcalifornia.com/location"
SEARCH_API = "pces/query"
DEFAULT_NETWORK = "PPO - Large Group/Small Group"
NPI_API = "https://npiregistry.cms.hhs.gov/api/"


def is_pces(body) -> bool:
    return isinstance(body, dict) and any(isinstance(r, dict) and "carrierEntity" in r
                                          for r in body.get("results") or [])


def _par_in_year(cycles: list, year: int) -> bool:
    lo, hi = f"{year}-01-01", f"{year}-12-31"
    return any(str(c.get("status", "")).lower() == "par" and str(c.get("startDate", ""))[:10] <= hi
               and str(c.get("endDate", "") or "9999")[:10] >= lo for c in cycles or [])


def parse_pces(body: dict, year: int | None = None) -> list[dict]:
    """One row per NPI x location of every listing that is in network (Par) during `year`."""
    year = year or date.today().year
    rows = []
    for res in body.get("results") or []:
        ce = res.get("carrierEntity") if isinstance(res, dict) else None
        if not ce:
            continue
        nw = ce.get("network") or {}
        if not _par_in_year(nw.get("networkCycles"), year):
            continue
        product, carrier = nw.get("product") or {}, nw.get("carrier") or {}
        listing = " | ".join(x for x in (product.get("name"), carrier.get("name")) if x)
        npis = [str(n.get("number")) for n in ce.get("nationalProviderIdentifiers") or [] if n.get("number")]
        name = ce.get("providerName") or ""
        if not name and isinstance(ce.get("name"), dict):
            name = " ".join(x for x in (ce["name"].get("first"), ce["name"].get("last")) if x)
        tin = "".join(ch for ch in str(ce.get("taxIdNumber") or "") if ch.isdigit())
        for npi in npis or [""]:
            for loc in ce.get("locations") or [{}]:
                a = loc.get("address") or {}
                specs = sorted({s.get("descriptionDisplay") or s.get("description") or ""
                                for s in loc.get("specialties") or []} - {""})
                rows.append({"npi": npi, "tin": tin or "".join(c for c in str(loc.get("taxIdNumber") or "") if c.isdigit()),
                             "name": name, "first_name": "", "last_name": "", "specialty": "|".join(specs[:5]),
                             "address_line1": a.get("addressLine1", ""), "address_line2": a.get("addressLine2", ""),
                             "city": a.get("city", ""), "state": a.get("stateCode", "") or a.get("state", ""),
                             "zip": str(a.get("zipCode", ""))[:5], "listing_networks": listing})
    return rows


def npi_city(npi: str, cache: dict, session: requests.Session) -> str:
    """'<City>, CA' from the NPI Registry practice location (cached)."""
    if npi not in cache:
        city = ""
        try:
            r = session.get(NPI_API, params={"version": "2.1", "number": npi}, timeout=30)
            res = (r.json().get("results") or [{}])[0]
            loc = next((a for a in res.get("addresses") or [] if a.get("address_purpose") == "LOCATION"), {})
            if loc.get("state") == "CA" and loc.get("city"):
                city = f"{loc['city'].title()}, CA"
        except Exception:
            pass
        cache[npi] = city
    return cache[npi]


def _setup(page, location: str, network: str, year: str) -> str:
    page.goto(SITE, wait_until="networkidle", timeout=90000)
    try:
        page.click("text=Accept", timeout=3000)  # cookie banner
    except Exception:
        pass
    box = page.get_by_label("Address, City, County or Zip Code")
    box.fill(location)
    page.wait_for_timeout(2500)
    box.press("ArrowDown")
    box.press("Enter")
    page.wait_for_timeout(1500)
    page.click('button:has-text("Continue")')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2500)
    radio = page.locator(f'md-radio-button[value="{year}"]')
    if radio.count():
        radio.first.click()
        page.wait_for_timeout(800)
    sel = page.locator("select").first
    opts = sel.evaluate("s => [...s.options].map(o => [o.value, o.text.trim()])")
    match = [o for o in opts if network.lower() in o[1].lower()]
    if not match:
        raise RuntimeError(f"network {network!r} not offered for {location} {year}")
    sel.select_option(value=match[0][0])
    page.wait_for_timeout(800)
    page.click('button:has-text("Continue")')
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2500)
    return match[0][1]


def fallback_term(tin_name: str, npi_name: str = "") -> str:
    """What to type for the name search: the rate file's TIN business name, cleaned. "THIELE, M.D." is
    a doctor's own corporation: search the surname. Placeholder names fall back to the NPPES name."""
    t = (tin_name or "").strip()
    if re.search(r",\s*(M\.?D|PH\.?D|PSY\.?D|M\.?F\.?T|D\.?O|D\.?D\.?S)\.?$", t, re.I):
        return t.split(",")[0].strip().title()
    if not t or t.upper().startswith("PPO:") or "HEALTH NET" in t.upper():
        t = npi_name or t
    t = re.sub(r",?\s*(INC\.?|LLC|L\.L\.C\.|APC|PC|A PROFESSIONAL MEDICAL CORPORATION|A PSYCHOLOGICAL CORPORATION)\s*$",
               "", t, flags=re.I).strip(" ,.")
    return t.title()


def _search(page, term: str) -> None:
    box = page.get_by_label("Search by Specialty", exact=False).first
    box.fill(term)
    page.wait_for_timeout(1500)
    box.press("Enter")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(5000)


def check(targets: list, out: Path, network: str = DEFAULT_NETWORK, year: str | None = None,
          location: str | None = None, executable: str | None = None, channel: str | None = None,
          delay: float = 1.0, fallback: bool = True, log=print) -> dict:
    """Search each NPI on the site under `network`; append the search responses to `out` (JSONL) and
    one coverage line per NPI to `<out>.coverage.jsonl`.

    `targets` are NPIs or dicts {npi, tin, tin_name}. With `fallback`, an NPI the site does not find is
    searched again by its TIN's business name, and listings with the same TIN are kept (a facility often
    bills under another NPI than the one the directory shows). Those captures carry `query_tin`, so
    `build` keeps only same-TIN listings from them."""
    from playwright.sync_api import sync_playwright

    from .capture import launch

    year = str(year or date.today().year)
    out.parent.mkdir(parents=True, exist_ok=True)
    cov_path = out.with_suffix(".coverage.jsonl")
    done = set()
    if cov_path.exists():
        for line in cov_path.read_text().splitlines():
            try:
                done.add(str(json.loads(line)["npi"]))
            except Exception:
                continue
    session, cities = requests.Session(), {}
    stats = {"searched": 0, "found": 0, "skipped_done": 0, "no_location": 0, "errors": 0}
    with open(out, "a", encoding="utf-8") as fh, open(cov_path, "a", encoding="utf-8") as cov, \
            sync_playwright() as pw:
        browser = launch(pw, channel, executable, headless=True)
        for t in targets:
            t = t if isinstance(t, dict) else {"npi": t}
            npi, tin = str(t["npi"]), re.sub(r"\D", "", str(t.get("tin") or ""))
            if npi in done:
                stats["skipped_done"] += 1
                continue
            loc = location or npi_city(npi, cities, session)
            if not loc:
                stats["no_location"] += 1
                cov.write(json.dumps({"npi": npi, "status": "no_ca_location"}) + "\n")
                continue
            ctx = browser.new_context(viewport={"width": 1300, "height": 900})
            page = ctx.new_page()
            armed, hits, net_label, query = [False], [], [""], {"kind": "npi"}

            def on_response(resp):
                if not armed[0] or SEARCH_API not in resp.url:
                    return
                try:
                    body = resp.json()
                except Exception:
                    return
                rec = {"t": datetime.now(timezone.utc).isoformat(), "url": resp.url, "method": resp.request.method,
                       "status": resp.status, "network_name": net_label[0], "query_npi": npi,
                       "query_location": loc, "network_year": year, "query_kind": query["kind"], "body": body}
                if query["kind"] == "name":
                    rec.update(query_tin=tin, query_term=query.get("term", ""))
                fh.write(json.dumps(rec, default=str) + "\n")
                fh.flush()
                if is_pces(body):
                    hits.extend(parse_pces(body, int(year)))

            ctx.on("response", on_response)
            status, fb = "not_found", {}
            try:
                net_label[0] = _setup(page, loc, network, year)
                armed[0] = True
                _search(page, npi)
                if any(h["npi"] == npi for h in hits):
                    status = "found"
                    stats["found"] += 1
                elif fallback and tin and (t.get("tin_name") or t.get("name")):
                    term = fallback_term(t.get("tin_name", ""), t.get("name", ""))
                    query.update(kind="name", term=term)
                    hits.clear()
                    armed[0] = False
                    page = ctx.new_page()  # a fresh search from the start: location, year, network
                    _setup(page, loc, network, year)
                    armed[0] = True
                    _search(page, term)
                    same = [h for h in hits if h["tin"] == tin]
                    fb = {"fallback_term": term, "fallback_same_tin": len(same),
                          "fallback_npis": sorted({h["npi"] for h in same if h["npi"]}),
                          "fallback_names": sorted({h["name"] for h in same if h["name"]})[:5]}
                    if same:
                        stats["found_by_tin"] = stats.get("found_by_tin", 0) + 1
            except Exception as exc:
                status = f"error: {exc.__class__.__name__}"
                stats["errors"] += 1
            stats["searched"] += 1
            cov.write(json.dumps({"npi": npi, "tin": tin, "status": status, "location": loc,
                                  "network": net_label[0], "year": year, **fb}) + "\n")
            cov.flush()
            log(f"[dir] {npi} {loc}: {status}" + (f"; by name {fb['fallback_term']!r}: {fb['fallback_same_tin']} "
                                                  f"listing(s) with the same TIN" if fb else ""))
            ctx.close()
            time.sleep(delay)
        browser.close()
    return stats
