"""C4-C7 tests on the miniature California world in ca_world.py, where every right answer is known."""
import csv
import gzip
import http.server
import json
import sqlite3
import threading
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import openpyxl
import pytest

import ca_world
from ptd_c3.tags import load_tag_table
from ptd_c4.cli import main as c4
from ptd_c4.directory import crawl_plannet
from ptd_c5.address import standardize
from ptd_c5.cli import main as c5
from ptd_c5.fetch import extract_nppes
from ptd_c6.cli import main as c6
from ptd_c7.bench import group_stats
from ptd_c7.cli import main as c7
from ptd_run.cli import main as run_all

AS_OF = "2026-10-17"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("ca")
    w = ca_world.build(root / "inputs")
    out = root / "out"
    db = str(out / "rates.sqlite")
    assert c4(["run", "--files", str(w["rate_file"]), "--workbook", str(w["workbook"]), "--directory",
               str(w["directory"]), "--db", db, "--out", str(out / "c4"), "--workers", "2", "--as-of", AS_OF]) == 0
    assert c5(["run", "--db", db, "--nppes", str(w["nppes"]), "--nppes-pl", str(w["pl"]), "--dac", str(w["dac"]),
               "--directory", str(w["directory"]), "--zip-county", str(w["zip_county"]), "--out", str(out / "c5"),
               "--as-of", AS_OF]) == 0
    assert c6(["run", "--db", db, "--workbook", str(w["workbook"]), "--out", str(out / "c6")]) == 0
    assert c7(["run", "--db", db, "--workbook", str(w["workbook"]), "--c6-out", str(out / "c6"),
               "--out", str(out / "c7")]) == 0
    w.update(out=out, db=db, con=sqlite3.connect(db))
    yield w
    w["con"].close()


def rows(con, q, *a):
    return con.execute(q, a).fetchall()


# ------------------------------------------------------------------ C4: network tags
def test_no_reference_is_tagged_with_the_wrong_network(world):
    truth = world["truth"]["ref_network"]
    wrong = [(ref, tag) for ref, tag in rows(world["con"], "SELECT ref_id, tag FROM c4_refs")
             if (truth[int(ref)] == "PPO" and tag == "NON_PPO") or (truth[int(ref)] == "HMO" and tag == "PPO")]
    assert wrong == []
    tags = dict(rows(world["con"], "SELECT tag, COUNT(*) FROM c4_refs GROUP BY tag"))
    assert tags["NON_PPO"] == 50          # every HMO reference found, from capitation, exclusion and spreading
    assert tags["PPO"] >= 20


def test_hypotheses_are_measured_before_use(world):
    s = json.loads((world["out"] / "c4/c4_run_summary.json").read_text())
    rep = next(iter(s["network"].values()))
    assert rep["propagation"]["used"] and rep["propagation"]["holdout_accuracy"] >= 0.9
    assert rep["dual_contract"]["precision"] == 1.0          # tested, but on fewer pairs than min_tested
    assert rep["dual_contract"]["used"] is False


def test_shared_npi_directory_listing_goes_to_the_reference_not_excluded(world):
    ev = dict(rows(world["con"], "SELECT ref_id, evidence FROM c4_refs WHERE ref_id IN ('1', '71')"))
    assert "their other reference(s) 71 are NON_PPO" in ev["1"]
    assert "capitation" in ev["71"]


def test_hmo_references_are_not_extracted(world):
    hmo_tins = {str(950000000 + g) for g in range(41, 71)}
    tins = {t for (t,) in rows(world["con"], "SELECT DISTINCT tin FROM c4_pairs")}
    assert not (tins & hmo_tins)
    assert "950000099" not in tins                            # TIN-only group: no NPI, no rows


def test_variants_are_split_and_junk_dropped(world):
    con, npi = world["con"], world["truth"]["tin_npis"]["950000002"]
    n = sorted(npi)[0]
    got = {(r[0], r[1], r[2]): r[3] for r in rows(
        con, "SELECT billing_class, negotiated_type, modifier, rate FROM c4_rates WHERE npi = ? AND code = '99213'", n)}
    assert got[("professional", "negotiated", "")] == 80.0    # zero and expired rows dropped, repeat merged
    assert got[("professional", "negotiated", "25")] == 90.0
    assert got[("institutional", "negotiated", "")] == 300.0
    assert got[("professional", "percentage", "")] == 150.0


def test_competing_rates_use_the_stated_tie_break(world):
    n = sorted(world["truth"]["tin_npis"]["950000006"])[0]
    r = rows(world["con"], "SELECT rate, distinct_rates, pick_rule FROM c4_rates WHERE npi = ? AND code = '99213' "
             "AND billing_class = 'professional' AND modifier = '' AND negotiated_type = 'negotiated'", n)
    assert r == [(80.0, 2, "lowest")]


def test_code_scope_and_capitation(world):
    codes = {c for (c,) in rows(world["con"], "SELECT DISTINCT code FROM c4_sig_rates")}
    assert codes == set(ca_world.PPO_CODES)                    # 11111 and 99499 are outside the workbook


def test_reference_order_and_worker_count_do_not_change_rates(world, tmp_path):
    def rates(path, workers, codes="all"):
        db = tmp_path / f"r{workers}.sqlite"
        assert c4(["run", "--files", str(path), "--codes", codes, "--directory", str(world["directory"]), "--db", str(db),
                   "--out", str(tmp_path / f"o{workers}"), "--workers", str(workers), "--as-of", AS_OF]) == 0
        con = sqlite3.connect(db)
        out = sorted(con.execute("SELECT npi, tin, code, negotiated_type, billing_class, modifier, rate FROM c4_rates"))
        con.close()
        return out
    a = rates(world["rate_file"], 1)
    b = rates(world["late_file"], 3)
    assert a == b and len(a) > 0
    assert not [r for r in a if r[2] == "99499"]               # capitation rows are never extracted


def test_provider_tags_feed_c3(world):
    t = load_tag_table(world["out"] / "c4/c4_provider_tags.csv")
    assert t.by_tin["950000041"] == {"NON_PPO"}
    assert "PPO" in t.by_tin["950000001"]


# ------------------------------------------------------------------ C5: addresses
def test_every_resolved_pair_gets_its_tins_real_site(world):
    truth = world["truth"]["tin_site"]
    wrong = []
    for npi, tin, site in rows(world["con"], "SELECT npi, tin, site_key FROM c5_addresses WHERE rank = 1 AND address != ''"):
        t = truth[tin]
        if standardize(t[0], t[1], t[2], "CA", t[3]).site_key != site:
            wrong.append((npi, tin))
    assert wrong == []


def test_one_npi_two_tins_two_addresses(world):
    sp = world["special"]
    got = dict(rows(world["con"], "SELECT tin, address FROM c5_addresses WHERE npi = ? AND rank = 1", sp["two_tin_npi"]))
    a, b = sp["two_tin_tins"]
    assert got[a] != got[b]
    assert "IRVINE" in got[b] and "SAN DIEGO" in got[a]


def test_stale_deactivated_and_missing_npis(world):
    sp, con = world["special"], world["con"]
    stale = rows(con, "SELECT address, verified FROM c5_addresses WHERE npi = ? AND rank = 1", sp["stale_npi"])[0]
    assert "OLD MILL" not in stale[0] and stale[1]
    assert rows(con, "SELECT address, evidence FROM c5_addresses WHERE npi = ?", sp["deactivated_npi"]) == \
        [("", "NPI deactivated in NPPES")]
    missing = rows(con, "SELECT address, families FROM c5_addresses WHERE npi = ? AND rank = 1", sp["not_in_nppes"])[0]
    assert missing[0] and "tin_consensus" in missing[1]


def test_logic_file_and_stats(world):
    text = (world["out"] / "c5/c5_logic.md").read_text()
    for part in ("TIN consensus", "Group alignment", "Conflict rules", "Verified address", "This run"):
        assert part in text
    s = json.loads((world["out"] / "c5/c5_run_summary.json").read_text())
    assert s["pct_npis_with_address"] > 99 and s["pct_npis_with_verified_address"] > 90
    assert s["best_address_vs_nppes_practice"]["differs from NPPES"] > 0


def test_standardiser():
    a = standardize("1200 West Third Street, Suite 100", "", "Los Angeles", "ca", "900171234")
    b = standardize("Medical Plaza", "1200 W. 3rd St. #100", "LOS ANGELES", "CA", "90017")
    assert a.site_key == b.site_key == "1200|W 3RD ST|90017"
    assert a.unit == b.unit == "STE 100"
    assert standardize("P.O. Box 55", "", "Fresno", "CA", "93701").po_box


def test_c5_without_sources_leaves_pairs_unresolved(world, tmp_path):
    import shutil
    db = tmp_path / "r.sqlite"
    shutil.copy(world["db"], db)
    assert c5(["run", "--db", str(db), "--out", str(tmp_path / "c5"), "--as-of", AS_OF]) == 0
    s = json.loads((tmp_path / "c5/c5_run_summary.json").read_text())
    assert s["pct_npis_with_address"] == 0


# ------------------------------------------------------------------ C6: the rate file
def test_rate_file_columns_rows_and_headline(world):
    wb = openpyxl.load_workbook(world["out"] / "c6/c6_provider_rate_file.xlsx", read_only=True)
    ws = wb["Rates"]
    data = list(ws.iter_rows(values_only=True))
    assert list(data[0][:6]) == ["NPI", "TIN", "Address", "Billing Code", "Specialty", "Rate"]
    s = json.loads((world["out"] / "c6/c6_run_summary.json").read_text())
    assert len(data) - 1 == s["rows"] > 0
    h = data[0]
    by = defaultdict(list)
    for r in data[1:]:
        by[(r[h.index("NPI")], r[h.index("Billing Code")])].append(r)
    org = world["truth"]["org_npi"]["950000002"]
    person = sorted(world["truth"]["tin_npis"]["950000002"])[0]
    assert by[(person, "99213")][0][h.index("Rate")] == 80.0     # Type 1: professional, no modifier, office
    assert by[(org, "99213")][0][h.index("Rate")] == 300.0       # Type 2: institutional when it exists
    assert by[(person, "99213")][0][h.index("Price Variants for Code")] == 4


def test_specialty_sources_and_plausibility(world):
    prov = {int(r["npi"]): r for r in csv.DictReader(open(world["out"] / "c6/c6_providers.csv"))}
    pcp = next(n for n, t in world["truth"]["npi_tax"].items() if t == ca_world.TAX_PCP and n in prov)
    assert prov[pcp]["specialty"] == "Primary Care" and prov[pcp]["specialty_source"] == "taxonomy_primary"
    assert prov[world["special"]["not_in_nppes"]]["specialty_source"] == "inferred_from_codes"
    exp = dict(rows(world["con"], "SELECT code, code_expected FROM c6_rates WHERE npi = ?", pcp))
    assert exp["99213"] == 1 and exp["93000"] == 0


# ------------------------------------------------------------------ C7: cost effectiveness
def test_cost_index_follows_the_true_price_tiers(world):
    idx = defaultdict(list)
    for r in csv.DictReader(open(world["out"] / "c7/c7_provider_scores.csv")):
        g = int(r["tin"]) - 950000000
        if g <= 40:
            idx[world["truth"]["tier"][g]].append(float(r["cost_index"]))
    means = [np.mean(idx[t]) for t in (0.8, 1.0, 1.2, 1.4)]
    assert means == sorted(means) and means[0] < 0.9 < 1.1 < means[3]


def test_group_stats_quantiles_and_tied_ranks():
    gid = np.array([0, 0, 0, 0, 1, 1])
    vals = np.array([10.0, 20.0, 20.0, 40.0, 5.0, 7.0])
    size, q1, med, q3, pct = group_stats(gid, vals, 2)
    assert list(size) == [4, 4, 4, 4, 2, 2]
    assert med[0] == 20.0 and q1[0] == 17.5 and q3[0] == 25.0 and med[4] == 6.0
    assert list(pct[:4]) == [0.0, 0.5, 0.5, 1.0]


def test_metrics_file_and_scored_rate_file(world):
    text = (world["out"] / "c7/c7_metrics.md").read_text()
    for part in ("similar providers", "Rate index", "shrinkage", "This run"):
        assert part in text
    s7 = json.loads((world["out"] / "c7/c7_run_summary.json").read_text())
    s6 = json.loads((world["out"] / "c6/c6_run_summary.json").read_text())
    assert s7["scored_rate_file_rows"] == s6["rows"]
    with gzip.open(world["out"] / "c7/c7_provider_rate_file_scored.csv.gz", "rt") as fh:
        head = next(csv.reader(fh))
    assert head[-4:] == ["Provider Cost Index", "Provider Cost Score", "Provider Tier", "Score Confidence"]


# ------------------------------------------------------------------ the runner and the summary
def test_runner_writes_summary_and_deliverables(world, tmp_path):
    w = world
    code = run_all(["all", "--from", "c3", "--files", str(w["rate_file"]), "--workbook", str(w["workbook"]),
                    "--directory", str(w["directory"]), "--nppes", str(w["nppes"]), "--nppes-pl", str(w["pl"]),
                    "--dac", str(w["dac"]), "--workers", "2", "--as-of", AS_OF, "--grade-by-network",
                    "--out", str(tmp_path / "out"), "--data", str(tmp_path / "data")])
    assert code == 0
    text = (tmp_path / "out/summary.md").read_text()
    assert "% NPIs linked to a verified address (C5)" in text and "| Unique NPIs (C4) | 160 |" in text
    names = {p.name for p in (tmp_path / "out/deliverables").iterdir()}
    assert {"1_provider_rate_file.xlsx", "2_logic_file.md", "3_cost_effectiveness_metrics.md", "4_summary.md"} <= names
    by_net = list(csv.DictReader(open(tmp_path / "out/c3_by_network/c3_file_scores.csv")))
    assert {"ALL", "PPO", "NON_PPO"} <= {r["bucket"] for r in by_net}


# ------------------------------------------------------------------ public-source adapters (offline)
def _serve(routes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            body = routes(u.path, parse_qs(u.query), f"http://127.0.0.1:{self.server.server_address[1]}")
            data = json.dumps(body).encode()
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_fhir_plannet_crawler(tmp_path):
    from ptd_c2.config import load_config

    def routes(path, q, base):
        if path.endswith("/Organization"):
            return {"entry": [{"resource": {"resourceType": "Organization", "id": "n1", "name": "PPO - Large Group"}},
                              {"resource": {"resourceType": "Organization", "id": "n2", "name": "HMO - WholeCare"}}]}
        if path.endswith("/PractitionerRole"):
            net = q["network"][0].split("/")[-1]
            if "page" not in q:
                return {"entry": [
                    {"resource": {"resourceType": "PractitionerRole", "practitioner": {"reference": "Practitioner/p1"},
                                  "location": [{"reference": "Location/l1"}], "meta": {"lastUpdated": "2026-09-01T00:00:00Z"}}},
                    {"resource": {"resourceType": "Practitioner", "id": "p1", "identifier": [
                        {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1234567893" if net == "n1" else "1000000004"}]}},
                    {"resource": {"resourceType": "Location", "id": "l1", "address": {
                        "line": ["455 N Brand Blvd"], "city": "Glendale", "state": "CA", "postalCode": "91203"}}}],
                    "link": [{"relation": "next", "url": f"{base}/fhir/PractitionerRole?network={q['network'][0]}&page=2"}]}
            return {"entry": [{"resource": {"resourceType": "PractitionerRole", "practitioner": {"reference": "Practitioner/x"},
                                            "location": [{"reference": "Location/l9"}]}},
                              {"resource": {"resourceType": "Practitioner", "id": "x", "identifier": [
                                  {"system": "http://hl7.org/fhir/sid/us-npi", "value": "1999999999"}]}},
                              {"resource": {"resourceType": "Location", "id": "l9", "address": {
                                  "line": ["1 Elm"], "city": "Reno", "state": "NV", "postalCode": "89501"}}}]}
        if path.endswith("/OrganizationAffiliation"):
            return {"entry": []}
        return None
    srv, base = _serve(routes)
    try:
        n = crawl_plannet(base + "/fhir", tmp_path / "dir.csv", load_config(), log=lambda m: None)
    finally:
        srv.shutdown()
    got = list(csv.DictReader(open(tmp_path / "dir.csv")))
    assert n == 2 and {r["network_label"] for r in got} == {"PPO_FULL", "HMO"}
    assert all(r["state"] == "CA" for r in got)                     # the Nevada location on page 2 is dropped


def test_npi_registry_api_fallback(world, tmp_path):
    import shutil
    n = world["special"]["not_in_nppes"]

    def routes(path, q, base):
        return {"results": [{"enumeration_type": "NPI-1", "basic": {"last_updated": "2026-08-01", "first_name": "A",
                                                                     "last_name": "B"},
                             "taxonomies": [{"code": ca_world.TAX_CARD, "primary": True}],
                             "addresses": [{"address_purpose": "LOCATION", "address_1": "455 N Brand Blvd",
                                            "city": "Glendale", "state": "CA", "postal_code": "91203"}]}]} \
            if q.get("number") == [str(n)] else {"results": []}
    srv, base = _serve(routes)
    db = tmp_path / "r.sqlite"
    shutil.copy(world["db"], db)
    cfg = tmp_path / "c5.yaml"
    text = (Path(__file__).resolve().parent.parent / "config/c5.yaml").read_text()
    cfg.write_text(text.replace("https://npiregistry.cms.hhs.gov/api/", base + "/api/").replace("requests_per_second: 5",
                                                                                               "requests_per_second: 1000"))
    try:
        assert c5(["run", "--db", str(db), "--nppes", str(world["nppes"]), "--npi-api", "10", "--config", str(cfg),
                   "--out", str(tmp_path / "c5"), "--as-of", AS_OF]) == 0
    finally:
        srv.shutdown()
    con = sqlite3.connect(db)
    assert con.execute("SELECT primary_taxonomy, source FROM c5_providers WHERE npi = ?", (n,)).fetchone() == \
        (ca_world.TAX_CARD, "npi_api")


def test_extract_nppes_state_from_monthly_zip(world, tmp_path):
    z = tmp_path / "NPPES_Data_Dissemination_October_2026.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(world["nppes"], "npidata_pfile_20050523-20261012.csv")
        zf.write(world["nppes"], "npidata_pfile_20050523-20261012_fileheader.csv")
        zf.write(world["pl"], "pl_pfile_20050523-20261012.csv")
    main, pl = extract_nppes(z, "CA", tmp_path / "ext", log=lambda m: None)
    n_src = sum(1 for _ in open(world["nppes"])) - 1
    assert sum(1 for _ in open(main)) - 1 == n_src and pl.exists()
    assert extract_nppes(z, "NV", tmp_path / "nv", log=lambda m: None)[0].read_text().count("\n") == 1
