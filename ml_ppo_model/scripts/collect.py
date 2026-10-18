"""Collect Health Net directory network profiles per NPI (all networks), politely.
usage: collect.py npis <npi_list.txt> <out.jsonl>     profile each NPI
       collect.py sample <out_npis.txt> <n_queries>     random CA commercial-medical listings -> NPI list
"""
import json, random, sys, time, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from q import q

TODAY = date(2026, 10, 18).isoformat()
MED_GR = ["8252","8219","8245","8247","8250","8249","8237","8236","8248","8238","8239","8242","9716","8241",
          "9676","8240","8243","8246","8218","8244","8216","8217","8251","8253"]
lock = threading.Lock()
PAGE = 200

def par_now(cycles):
    for c in cycles or []:
        if c.get("status") == "Par" and (c.get("startDate") or "")[:10] <= TODAY <= (c.get("endDate") or "9999")[:10]:
            return True
    return False

def slim(rec, npi):
    ce = rec["carrierEntity"]; n = ce.get("network") or {}
    locs = ce.get("locations") or []
    return {"carrier": (n.get("carrier") or {}).get("networkId"), "carrier_name": (n.get("carrier") or {}).get("name"),
            "bu": (n.get("businessUnit") or {}).get("code"), "par": par_now(n.get("networkCycles")),
            "npis": [(x.get("number"), x.get("type")) for x in ce.get("nationalProviderIdentifiers") or []],
            "tin": ce.get("taxIdNumber"), "loc_tins": sorted({l.get("taxIdNumber") for l in locs if l.get("taxIdNumber")}),
            "ptype": ce.get("providerType"), "name": ce.get("providerName"),
            "cap": [p.get("capitatedPPG") for p in ce.get("participatingPhysicianGroupIdentifiers") or []],
            "ppg": [p.get("ppgId") for p in ce.get("participatingPhysicianGroupIdentifiers") or []],
            "spec": sorted({s.get("description") for l in locs for s in l.get("specialties") or [] if s.get("description")}),
            "tax": [f.get("value") for a in ce.get("attributes") or [] if a.get("name") == "Provider Taxonomy" for f in a.get("fields") or []],
            "zips": sorted({(l.get("address") or {}).get("zipCode", "")[:5] for l in locs}),
            "states": sorted({(l.get("address") or {}).get("stateCode", "") for l in locs}),
            "display": any(e.get("eligibleIndicator") == "Y" and (e.get("startDate") or "")[:10] <= TODAY <= (e.get("endDate") or "9999")[:10]
                           for l in locs for e in l.get("eligibleForDisplay") or []),
            "excluded": any(c.get("excludeFromDirectory") == "Y" for l in locs for c in l.get("networkLocationCycles") or []),
            "entity": ce.get("entityType")}

def profile(npi):
    out, frm = [], 0
    while True:
        for attempt in range(4):
            try:
                d = q({"customQuery": {"constantScore": {"boost": 1, "query": {"values": {
                    "field": "nationalProviderIdentifiers.number", "values": [npi]}}}}, "size": PAGE, "from": frm})
                break
            except Exception as e:
                time.sleep(2 ** attempt * 2)
        else:
            return {"npi": npi, "error": True, "records": out}
        out += [slim(r, npi) for r in d.get("results", [])]
        frm += PAGE
        if frm >= d.get("totalHits", 0) or frm >= 800:
            return {"npi": npi, "total": d.get("totalHits", 0), "records": out}

def run_npis(listfile, outfile, workers=3):
    npis = [l.strip() for l in open(listfile) if l.strip()]
    done = set()
    try:
        done = {json.loads(l)["npi"] for l in open(outfile)}
    except FileNotFoundError:
        pass
    todo = [n for n in npis if n not in done]
    print(f"{len(todo)} to do, {len(done)} done", flush=True)
    f = open(outfile, "a")
    def job(n):
        r = profile(n); time.sleep(0.3)
        with lock:
            f.write(json.dumps(r) + "\n"); f.flush()
    with ThreadPoolExecutor(workers) as ex:
        for i, _ in enumerate(ex.map(job, todo)):
            if i % 200 == 0: print(i, time.strftime("%H:%M:%S"), flush=True)
    f.close()

# CA population centres (lat, lon) spread across regions
POINTS = [(34.05,-118.24),(34.15,-118.14),(34.19,-118.45),(33.77,-118.19),(33.84,-117.91),(33.69,-117.83),(34.11,-117.29),
          (33.95,-117.40),(33.72,-116.37),(32.72,-117.16),(33.12,-117.09),(32.64,-117.08),(37.77,-122.42),(37.80,-122.27),
          (37.55,-122.31),(37.34,-121.89),(37.66,-121.89),(38.58,-121.49),(38.75,-121.29),(36.74,-119.79),(35.37,-119.02),
          (37.64,-120.99),(37.96,-121.29),(36.60,-121.89),(34.42,-119.70),(34.28,-119.29),(38.44,-122.71),(38.30,-122.29),
          (40.59,-122.39),(39.73,-121.84),(34.58,-118.12),(36.33,-119.29),(35.28,-120.66),(34.48,-117.33),(33.20,-117.24)]

def run_sample(outfile, nq, seed=7):
    rnd = random.Random(seed); npis = {}
    for i in range(nq):
        lat, lon = rnd.choice(POINTS)
        lat += rnd.uniform(-0.08, 0.08); lon += rnd.uniform(-0.08, 0.08)
        d = None
        for radius in ("0.5mi", "0.2mi", "0.08mi"):
            body = {"customQuery": {"requireAll": {"criteria": [
                {"nested": {"path": "locations", "query": {"constantScore": {"boost": 1, "query": {"geoFilter": {
                    "field": "locations.address.search_coordinates", "radius": {"coordinates": f"{lat}, {lon}", "distance": radius}}}}}}},
                {"constantScore": {"boost": 1, "query": {"values": {"field": "network.carrier.networkId", "values": MED_GR}}}}]}},
                "size": 50, "from": 0}
            try:
                tot = q(dict(body, size=1))["totalHits"]
            except Exception as e:
                print("err", e); time.sleep(5); tot = None; break
            if tot <= 550:
                break
        if not tot or tot > 550:
            continue
        try:
            body["from"] = rnd.randrange(0, max(1, tot - 50 + 1)) if tot > 50 else 0
            d = q(body)
        except Exception as e:
            print("err", e); time.sleep(5); continue
        res = d.get("results") or []
        for r in rnd.sample(res, min(3, len(res))):   # up to 3 random listings per query
            for num, typ in [(x.get("number"), x.get("type")) for x in r["carrierEntity"].get("nationalProviderIdentifiers") or []]:
                if num not in npis:
                    npis[num] = typ
                    with open(outfile, "a") as f:
                        f.write(f"{num}\t{typ}\t{tot}\n")
                break
        time.sleep(0.3)
        if i % 100 == 0: print(i, len(npis), flush=True)
    print("sampled", len(npis))

if __name__ == "__main__":
    if sys.argv[1] == "npis": run_npis(sys.argv[2], sys.argv[3])
    else: run_sample(sys.argv[2], int(sys.argv[3]))
