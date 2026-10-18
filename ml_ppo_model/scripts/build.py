"""Turn directory profiles into one row per NPI with labels and features."""
import json, os, sys, csv, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline"))
import yaml
from ptd_c5.resolve import region_for
FAMILIES = [
    ("behavioural", r"psych|counsel|therapist|social work|behavio|marriage|mental|addiction|substance"),
    ("hospital_based", r"emergency|radiolog|anesthes|patholog|hospitalist|neonat|critical care"),
    ("facility", r"facility|hospital|center|clinic|laborator|ambulatory|pharmac|skilled|urgent|home health|supplier|dme|261Q|282N|291U|3336|332B"),
    ("primary_care", r"primary care|family|internal medicine|general practice|pediatric"),
    ("therapy", r"physical therap|occupational|speech|chiropr|podiat|audiolog"),
    ("surgical", r"surg|orthop|urolog|otolaryng|ophthalm|obstet|gynec"),
    ("medical", r"cardio|dermat|gastro|neuro|oncol|endocr|nephro|pulmon|rheumat|allerg|infect|hematol"),
]
def family(s):
    s = str(s or "")
    for n, rx in FAMILIES:
        if re.search(rx, s, re.I): return n
    return "other"
REG = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pipeline", "config", "c5.yaml")))["regions"]
PPO = "8242"
HMO = {"8252","8250","8237","8236","8248","8246","8218","8216","8217","8253","8238"}
EPO_HSP = {"9716","9676","8241","8240","8219","8245","8247"}   # EPO, HSP and Elect Open Access (HMO-based)
MED_GR = {"8252","8219","8245","8247","8250","8249","8237","8236","8248","8238","8239","8242","9716","8241","9676","8240","8243","8246","8218","8244","8216","8217","8251","8253"}

def row(p):
    npi = p["npi"]; recs = p["records"]
    own = [r for r in recs if any(n == npi for n, _ in r["npis"])]
    par = {r["carrier"] for r in own if r["par"]}
    gr = [r for r in own if r["carrier"] in MED_GR]
    types = {t for r in own for n, t in r["npis"] if n == npi}
    spec = sorted({s for r in gr for s in r["spec"]})
    tax = sorted({t for r in gr for t in r["tax"] if t})
    zips = [z for r in gr for z in r["zips"] if z]
    tins = {t for r in gr for t in [r["tin"]] + r["loc_tins"] if t}
    return {"npi": npi, "error": int(bool(p.get("error"))),
            "listed_any": int(bool(par)), "listed_commercial": int(bool(par & MED_GR)),
            "ppo": int(PPO in par),
            "ppo_displayed": int(any(r["carrier"] == PPO and r["par"] and r.get("display") and not r.get("excluded") for r in own)), "hmo": int(bool(par & HMO)), "epo_hsp_eoa": int(bool(par & EPO_HSP)),
            "n_commercial_networks": len(par & MED_GR), "n_gr_records": len(gr),
            "npi_type_2": int("Type 2" in types) if types else "",
            "ptype": "|".join(sorted({r["ptype"] or "" for r in gr})),
            "specialty": "; ".join(spec)[:200], "taxonomy": "|".join(tax),
            "family": family(" ".join(spec) + " " + " ".join(tax)) if (spec or tax) else "",
            "region": region_for(zips[0], REG) if zips else "",
            "n_tins": len(tins), "tins": "|".join(sorted(tins)),
            "capitated_ppg": int(any(c == "Y" for r in gr for c in r["cap"])),
            "in_ppg": int(any(r["ppg"] for r in gr))}

if __name__ == "__main__":
    rows = [row(json.loads(l)) for l in open(sys.argv[1])]
    w = csv.DictWriter(open(sys.argv[2], "w", newline=""), fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)
    import collections
    print(len(rows), "rows; ppo", sum(r["ppo"] for r in rows), "hmo", sum(r["hmo"] for r in rows), "listed_commercial", sum(r["listed_commercial"] for r in rows))
