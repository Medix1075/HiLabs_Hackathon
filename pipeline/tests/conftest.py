"""Builds a miniature Centene world (index, rate files, OON files, C1 table) and serves it over HTTP.

The plan names are real Health Net / Ambetter / QualChoice names quoted in the research notes;
the rate-file contents follow CMS schema v2.0.0 but are synthetic.
"""
from __future__ import annotations

import csv
import functools
import gzip
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

# Tests never call the live NPI Registry API; the one API test serves its own and passes --npi-api.
os.environ.setdefault("PTD_NPI_API", "off")
os.environ.setdefault("PTD_GEOCODE", "off")
# ...and never start the live Health Net directory search when no saved PPO check is in pipeline/inputs.
os.environ.setdefault("PTD_DIRECTORY_CHECK", "off")


def _refs(network_names, n_refs=60, tin_base=100000000, npis=3, as_list=True):
    refs = []
    for i in range(n_refs):
        nn = network_names[i % len(network_names)]
        refs.append({
            "provider_group_id": i + 1,
            "network_name": [nn] if as_list else nn,
            "provider_groups": [{"npi": [1000000000 + i * 10 + k for k in range(npis)],
                                 "tin": {"type": "ein", "value": str(tin_base + i)}}],
        })
    return refs


def _in_network(n=30, arrangement="ffs", cap_every=0):
    items = []
    for i in range(n):
        arr = "capitation" if cap_every and i % cap_every == 0 else arrangement
        items.append({
            "negotiation_arrangement": arr, "name": f"code {i}", "billing_code_type": "CPT",
            "billing_code_type_version": "2026", "billing_code": str(99200 + i), "description": "x",
            "negotiated_rates": [{"provider_references": [1, 2],
                                  "negotiated_prices": [{"negotiated_type": "negotiated", "negotiated_rate": 100.0 + i,
                                                         "expiration_date": "9999-12-31", "billing_class": "professional"}]}],
        })
    return items


def _rate_file(refs, items, refs_first=True):
    head = {"reporting_entity_name": "Centene Management Company LLC", "reporting_entity_type": "Third Party Administrator",
            "last_updated_on": "2026-09-30", "version": "2.0.0"}
    body = {"provider_references": refs, "in_network": items}
    order = ["provider_references", "in_network"] if refs_first else ["in_network", "provider_references"]
    return {**head, **{k: body[k] for k in order}}


PPO_NAMES = ["CA L PPO 30/500/4000/30%", "CA L PPO 20/250/3000/20%", "CA L PPO 40/1000/5000/40%",
             "CA L PPO HSA ED 20%/5000/6000", "Gold PPO 500/20", "Platinum PPO 250/15", "CA L PPO 25/750/4500/25%",
             "CA L PPO 10/0/2000/10%", "CA L PPO 35/1500/6000/30%", "CA L PPO 15/500/3500/20%",
             "CA L PPO 45/2000/7000/40%", "CA L PPO 30/1000/5000/30%"]
HMO_NAMES = ["CA L HMO 40/1500d/6500", "CA L SC HMO 50/1500d/7500", "CA L SAL HMO 20/20%/2500",
             "WholeCare HMO Gold $50 INF", "Full Network HMO Gold $30", "Salud HMO y Mas Silver $55 INF",
             "CA L HMO 30/1000d/5000", "CA L HMO 20/500d/4000", "CA L HMO 10/250d/3000", "CA L HMO 35/2000d/7000",
             "CA L HMO 25/750d/4500", "CA L HMO 15/0d/2500"]


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("centene")
    files = root / "files"
    files.mkdir()

    def dump(name, obj, gz=False):
        data = json.dumps(obj).encode()
        if gz:
            (files / name).write_bytes(gzip.compress(data))
        else:
            (files / name).write_bytes(data)

    # Rate files
    dump("hn_ppo_in-network.json.gz", _rate_file(_refs(["PPO - Large Group/Small Group"], 80), _in_network(30)), gz=True)
    dump("hn_hmo_in-network.json", _rate_file(_refs(["HMO - Full Network", "HMO - WholeCare"], 40, npis=60),
                                             _in_network(30, cap_every=3)))
    dump("hn_ec_in-network.json", _rate_file(_refs(["EnhancedCare PPO"], 20, as_list=False), _in_network(25)))
    dump("hn_mixed_in-network.json", _rate_file(_refs(["PPO - Large Group/Small Group", "HMO - SmartCare"], 30),
                                               _in_network(25)))
    # in_network before provider_references: must still find network names
    dump("hn_late_in-network.json", _rate_file(_refs(["PPO - Large Group/Small Group"], 10), _in_network(300),
                                              refs_first=False))
    # provider_references given by location
    dump("hn_prov_refs.json", {"provider_groups": [{"npi": [1], "tin": {"type": "ein", "value": "999"}}],
                               "network_name": ["HSP - PureCare"]})
    dump("hn_loc_in-network.json", _rate_file([{"provider_group_id": 1, "location": "{BASE}/hn_prov_refs.json"}],
                                             _in_network(5)))
    # File that names HMO network although its plans look PPO (network_name must win)
    dump("hn_trap_in-network.json", _rate_file(_refs(["HMO - CommunityCare"], 10), _in_network(25)))
    # Like Health Net's real CA file: network_name present on every reference but blank
    dump("hn_blank_in-network.json", _rate_file(_refs([""], 30), _in_network(25)))
    dump("qc_in-network.json", _rate_file(_refs(["QualChoice PPO"], 5), _in_network(5)))
    dump("ambetter-ok_in-network.json", _rate_file(_refs(["Ambetter Balanced Care"], 5), _in_network(5)))

    # OON files
    dump("hn_ppo_allowed-amounts.json", {"reporting_entity_name": "x", "out_of_network": [
        {"name": "a", "billing_code": "99213", "allowed_amounts": []}]})
    dump("hn_hmo_allowed-amounts.json", {"reporting_entity_name": "x", "out_of_network": []})

    plans = []  # (brand, name, plan_id, market, rate_file, oon_file)
    for i, n in enumerate(PPO_NAMES):
        plans.append(("Health Net", n, f"95278{9000 + i}", "group", "hn_ppo_in-network.json.gz", "hn_ppo_allowed-amounts.json"))
    for i, n in enumerate(HMO_NAMES):
        plans.append(("Health Net", n, f"95278{9000 + i}", "group", "hn_hmo_in-network.json", "hn_hmo_allowed-amounts.json"))
    plans += [
        # Unlabelled name: inherits the PPO file's label
        ("Health Net", "CA L CUS ACME CORP 20/250/3000/20%", "999999999", "group", "hn_ppo_in-network.json.gz", "hn_ppo_allowed-amounts.json"),
        ("Health Net", "`CA L CUS PPO CA IRONWORKERS FWP EPO$15 ", "941234567", "group", "hn_ppo_in-network.json.gz", ""),
        ("Health Net", "CA L EC HMO 50/1500d/7500", "952789166", "group", "hn_hmo_in-network.json", "hn_hmo_allowed-amounts.json"),
        ("Health Net", "CA L EC 30/500/4000/30%", "952789166", "group", "hn_ec_in-network.json", ""),
        ("Health Net", "CA L CUS POS COUNTY OF SANTA CLARA", "946000533", "group", "hn_mixed_in-network.json", ""),
        ("Health Net", "CA L PPO MIXEDCO 30/500/4000/30%", "940000001", "group", "hn_mixed_in-network.json", ""),
        ("Health Net", "CA L SC HMO MIXEDCO 40/1500d/6500", "940000001", "group", "hn_mixed_in-network.json", ""),
        ("Health Net", "CA L PPO LATECO 30/500/4000/30%", "940000002", "group", "hn_late_in-network.json", ""),
        ("Health Net", "CA L&T CUS LOCCO", "940000003", "group", "hn_loc_in-network.json", ""),
        ("Health Net", "CA L PPO TRAPCO 30/500/4000/30%", "940000004", "group", "hn_trap_in-network.json", ""),
        # Real 2026-09-30 name shapes: EOA, EC next to EOA, CUSPPO without a space
        ("Health Net", "CA L EOA 20/500A/2500", "940000006", "group", "hn_blank_in-network.json", ""),
        ("Health Net", "CA L EC EOA 15/1500D/6500", "940000006", "group", "hn_blank_in-network.json", ""),
        ("Health Net", "CA L CUSPPO 15/250/20% RAP", "940000007", "group", "hn_blank_in-network.json", "hn_ppo_allowed-amounts.json"),
        ("Health Net", "CA L SAL Mex 5/0/1500", "940000005", "group", "hn_missing_in-network.json", ""),
        # Traps for the scope filter
        ("Ambetter", "Ambetter Health Solutions Gold PPO 0", "29276OK0010001", "individual", "ambetter-ok_in-network.json", ""),
        ("Fidelis", "Ambetter from Fidelis Care Silver", "25303NY0010001", "individual", "fidelis-ex_in-network.json", ""),
        ("QualChoice", "`PPO Complete - C1500 Freedom", "70525", "group", "qc_in-network.json", ""),
        ("QualChoice", "Bronze Enhanced HSA PPO w Ped Dental ", "37903AR0010001", "group", "qc_in-network.json", ""),
    ]

    # Raw Health Net index (only Health Net plans; carries allowed_amount_file)
    structures = {}
    for brand, name, pid, market, rf, oon in plans:
        if brand != "Health Net":
            continue
        s = structures.setdefault((rf, oon), {"reporting_plans": [], "in_network_files": [
            {"description": "in-network", "location": "{BASE}/" + rf}]})
        if oon:
            s["allowed_amount_file"] = {"description": "oon", "location": "{BASE}/" + oon}
        s["reporting_plans"].append({"plan_name": name, "plan_id_type": "ein", "plan_id": pid,
                                     "plan_market_type": market, "issuer_name": "Healthnet, LLC"})
    index = {"reporting_entity_name": "Centene Management Company LLC", "reporting_entity_type": "Third Party Administrator",
             "version": "2.0.0", "reporting_structure": list(structures.values())}

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(files))

    class Quiet(handler.func):
        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(files)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    # substitute {BASE} placeholders now that the port is known
    for f in files.iterdir():
        if f.suffix == ".json":
            f.write_text(f.read_text().replace("{BASE}", base))
    (root / "2026-09-30_healthnet_index.json").write_text(json.dumps(index).replace("{BASE}", base))

    c1 = root / "c1.csv"
    with open(c1, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["brand", "plan name", "plan ID", "market type", "rate file URL"])
        for brand, name, pid, market, rf, _ in plans:
            w.writerow([brand, name, pid, market, f"{base}/{rf}"])
        w.writerow(["Health Net", PPO_NAMES[0], "952789000", "group", f"{base}/hn_ppo_in-network.json.gz"])  # duplicate
    yield {"root": root, "c1": c1, "index": root / "2026-09-30_healthnet_index.json", "base": base}
    server.shutdown()
