import csv
import json
from pathlib import Path

import pytest

from ptd_c2.cli import main
from ptd_c2.config import load_config
from ptd_c2.name_signals import _strong_types, tag_name
from ptd_c2.network_probe import label_network_name
from ptd_c2.normalize import norm_plan_name, state_from_file_url, state_from_name, state_from_plan_id


def read(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def run_out(world, tmp_path_factory):
    out = tmp_path_factory.mktemp("out")
    assert main(["run", "--c1", str(world["c1"]), "--index", str(world["index"]), "--out", str(out)]) == 0
    rows = {r["plan_name"].strip().strip("`").strip(): r for r in read(out / "c2_classification.csv")}
    return out, rows


# ---------------- unit tests

def test_normalise_strips_backtick_and_spaces():
    assert norm_plan_name("`PPO Complete - C1500  Freedom ") == "PPO COMPLETE - C1500 FREEDOM"


def test_state_sources():
    assert state_from_name("CA L PPO 30/500") == "CA"
    assert state_from_name("CA L SC HMO") == "CA"
    assert state_from_name("SC HMO 40") is None          # SC is SmartCare, not South Carolina
    assert state_from_plan_id("67138CA060") == "CA"
    assert state_from_file_url("x/2026-09-28_centene-management-company-llc_ambetter-ok_in-network.json") == "OK"


@pytest.mark.parametrize("name,expected", [
    ("CA L PPO 30/500/4000/30%", {"PPO"}),
    ("CA L EC HMO 50/1500D/7500", {"HMO"}),                  # EC ignored next to HMO
    ("CA L EC 30/500/4000/30%", {"PPO"}),                    # EC alone -> EnhancedCare PPO
    ("CA L CUS PPO CA IRONWORKERS FWP EPO$15", {"PPO", "EPO"}),
    ("CA L SAL MEX 5/0/1500", {"HMO"}),
    ("PURECARE HSP SILVER", {"HSP"}),
    ("CA L CUS POS COUNTY OF SANTA CLARA", {"POS"}),
    ("CA L EOA 20/500A/2500", {"EOA"}),
    ("CA L EC EOA 15/1500D/6500", {"EOA"}),                  # EC ignored next to EOA
    ("CA L CUSPPO 15/250/20% RAP", {"PPO"}),
])
def test_strong_name_types(cfg, name, expected):
    assert _strong_types(tag_name(name, cfg))[0] == expected


@pytest.mark.parametrize("network,label", [
    ("PPO - Large Group/Small Group", "PPO_FULL"), ("PPO – Large Group/Small Group", "PPO_FULL"),
    ("Full PPO Network", "PPO_FULL"), ("EnhancedCare PPO", "PPO_ENHANCEDCARE"),
    ("HMO - Full Network", "HMO"), ("Full Network", "HMO"), ("WholeCare", "HMO"), ("HMO - Salud HMO y Más", "HMO"),
    ("HSP - PureCare", "HSP"), ("PureCare", "HSP"), ("Something Else", "UNKNOWN"),
])
def test_network_name_labels(cfg, network, label):
    assert label_network_name(network, cfg) == label


# ---------------- end-to-end over the fixture world

def test_scope_traps_excluded_by_rule_not_keyword(run_out):
    out, rows = run_out
    ex = {r["plan_name"].strip().strip("`").strip(): r for r in read(out / "c2_exclusions.csv")}
    assert ex["Ambetter Health Solutions Gold PPO 0"]["primary_rule"] == "market"
    assert ex["PPO Complete - C1500 Freedom"]["primary_rule"] == "brand"
    assert "state" in ex["PPO Complete - C1500 Freedom"]["all_rules_failed"]
    assert "AR" in ex["Bronze Enhanced HSA PPO w Ped Dental"]["reason"]
    assert all(rows[n]["in_scope_ppo"] == "False" for n in ex)


def test_duplicates_dropped(run_out):
    out, _ = run_out
    s = json.loads((out / "c2_run_summary.json").read_text())
    assert s["c1_stats"]["duplicates_dropped"] == 1


def test_clear_ppo_and_hmo(run_out):
    _, rows = run_out
    r = rows["CA L PPO 30/500/4000/30%"]
    assert (r["final_network_type"], r["in_scope_ppo"], r["confidence"], r["decided_by"]) == ("PPO", "True", "high", "network_name")
    assert r["ppo_variant"] == "FULL"
    h = rows["CA L HMO 40/1500d/6500"]
    assert (h["final_network_type"], h["in_scope_ppo"], h["confidence"]) == ("HMO", "False", "high")
    assert h["payment_signal"] == "HMO-leaning"
    assert rows["Gold PPO 500/20"]["state_source"].startswith("brand_default")


def test_unlabelled_plan_inherits_and_is_confirmed(run_out):
    _, rows = run_out
    r = rows["CA L CUS ACME CORP 20/250/3000/20%"]
    assert r["final_network_type"] == "PPO" and r["in_scope_ppo"] == "True"


def test_conflict_name_resolved_by_network_name(run_out):
    out, rows = run_out
    r = rows["CA L CUS PPO CA IRONWORKERS FWP EPO$15"]
    assert r["name_signal"] == "conflict" and r["final_network_type"] == "PPO"
    kinds = {c["conflict_type"] for c in read(out / "c2_conflicts.csv")
             if "IRONWORKERS" in c["plan_name"]}
    assert "name_conflict" in kinds


def test_enhancedcare(run_out):
    _, rows = run_out
    assert rows["CA L EC HMO 50/1500d/7500"]["final_network_type"] == "HMO"
    ec = rows["CA L EC 30/500/4000/30%"]
    assert (ec["final_network_type"], ec["ppo_variant"], ec["in_scope_ppo"]) == ("PPO", "ENHANCEDCARE", "True")


def test_enhancedcare_flag_off(world, tmp_path):
    cfg_text = (Path(__file__).resolve().parent.parent / "config" / "c2.yaml").read_text()
    cfg_path = tmp_path / "c2.yaml"
    cfg_path.write_text(cfg_text.replace("include_enhancedcare_ppo: true", "include_enhancedcare_ppo: false"))
    out = tmp_path / "out"
    main(["run", "--c1", str(world["c1"]), "--config", str(cfg_path), "--out", str(out)])
    rows = {r["plan_name"]: r for r in read(out / "c2_classification.csv")}
    assert rows["CA L EC 30/500/4000/30%"]["in_scope_ppo"] == "False"
    assert rows["CA L PPO 30/500/4000/30%"]["in_scope_ppo"] == "True"


def test_mixed_file_needs_filter(run_out):
    out, rows = run_out
    assert rows["CA L PPO MIXEDCO 30/500/4000/30%"]["final_network_type"] == "PPO"
    assert rows["CA L PPO MIXEDCO 30/500/4000/30%"]["needs_network_filter"] == "True"
    assert rows["CA L SC HMO MIXEDCO 40/1500d/6500"]["final_network_type"] == "HMO"
    assert rows["CA L CUS POS COUNTY OF SANTA CLARA"]["final_network_type"] == "POS"
    files = {f["rate_file_url"].rsplit("/", 1)[-1]: f for f in read(out / "c2_ppo_rate_files.csv")}
    assert files["hn_mixed_in-network.json"]["needs_network_filter"] == "True"
    assert files["hn_mixed_in-network.json"]["ppo_network_names"] == "PPO - Large Group/Small Group"


def test_in_network_before_refs_still_found(run_out):
    _, rows = run_out
    r = rows["CA L PPO LATECO 30/500/4000/30%"]
    assert r["decided_by"] == "network_name" and r["final_network_type"] == "PPO"


def test_location_reference_followed(run_out):
    _, rows = run_out
    r = rows["CA L&T CUS LOCCO"]
    assert r["network_names"] == "HSP - PureCare" and r["final_network_type"] == "HSP"


def test_network_name_beats_plan_name(run_out):
    out, rows = run_out
    r = rows["CA L PPO TRAPCO 30/500/4000/30%"]
    assert r["final_network_type"] == "HMO" and r["confidence"] == "medium" and r["in_scope_ppo"] == "False"
    assert any(c["conflict_type"] == "name_vs_network_name" and "TRAPCO" in c["plan_name"]
               for c in read(out / "c2_conflicts.csv"))


def test_unreachable_file_falls_back_to_name(run_out):
    _, rows = run_out
    r = rows["CA L SAL Mex 5/0/1500"]
    assert r["final_network_type"] == "HMO" and r["confidence"] == "medium" and r["decided_by"] == "plan name"


def test_supporting_oon(run_out):
    _, rows = run_out
    assert rows["CA L PPO 30/500/4000/30%"]["oon_signal"] == "PPO-leaning"
    assert rows["CA L HMO 40/1500d/6500"]["oon_signal"] == "HMO-leaning"


def test_every_row_has_reason_and_outputs_exist(run_out):
    out, rows = run_out
    assert all(r["reason"] for r in rows.values())
    for name in ("c2_ppo_rate_files.csv", "c2_hypotheses.csv", "c2_validation_sample.csv", "c2_results.xlsx",
                 "c2_network_probe.csv", "c2_run_log.txt"):
        assert (out / name).exists()
    ppo_files = {f["rate_file_url"].rsplit("/", 1)[-1] for f in read(out / "c2_ppo_rate_files.csv")}
    assert ppo_files == {"hn_ppo_in-network.json.gz", "hn_ec_in-network.json", "hn_mixed_in-network.json",
                         "hn_late_in-network.json", "hn_blank_in-network.json"}


def test_blank_network_names_and_shared_file(run_out):
    out, rows = run_out
    probe = {p["rate_file_url"].rsplit("/", 1)[-1]: p for p in read(out / "c2_network_probe.csv")}
    assert probe["hn_blank_in-network.json"]["status"] == "network_name_blank"
    assert probe["hn_blank_in-network.json"]["network_label"] == "UNCONFIRMED"
    r = rows["CA L CUSPPO 15/250/20% RAP"]
    assert (r["final_network_type"], r["decided_by"], r["confidence"], r["needs_network_filter"]) == \
        ("PPO", "plan name", "medium", "True")
    assert rows["CA L EOA 20/500A/2500"]["final_network_type"] == "EOA"
    assert rows["CA L EC EOA 15/1500D/6500"]["final_network_type"] == "EOA"


def test_hypotheses_measured(run_out):
    out, _ = run_out
    hyp = {h["hypothesis"]: h for h in read(out / "c2_hypotheses.csv")}
    assert hyp["shape:COINSURANCE_TAIL"]["used_in_classification"] == "True"
    assert hyp["hint:LIFE_ENTITY"]["used_in_classification"] == "False"


def test_offline_mode(world, tmp_path):
    main(["run", "--c1", str(world["c1"]), "--out", str(tmp_path), "--no-probe"])
    rows = {r["plan_name"]: r for r in read(tmp_path / "c2_classification.csv")}
    r = rows["CA L PPO TRAPCO 30/500/4000/30%"]
    assert r["final_network_type"] == "PPO" and r["confidence"] in ("low", "medium")
    assert all(x["confidence"] != "high" for x in rows.values() if x["final_network_type"] != "OUT_OF_SCOPE")


def test_validate_scoring(run_out, tmp_path):
    out, _ = run_out
    sample = read(out / "c2_validation_sample.csv")
    for i, r in enumerate(sample):
        r["source_label"] = r["our_label"] if i else "HMO plan"
    p = tmp_path / "filled.csv"
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(sample[0]))
        w.writeheader()
        w.writerows(sample)
    main(["validate", "--sample", str(p), "--out", str(tmp_path)])
    s = json.loads((tmp_path / "c2_validation_summary.json").read_text())
    assert s["checked"] == len(sample)


def test_c2_reads_c1_outputs_end_to_end(world, tmp_path, monkeypatch):
    """--run-c1: C1 scrapes a saved page, parses the cached raw indexes, then C2 reads its outputs."""
    import shutil

    from ptd_pipeline.scraper import parse_toc_links

    page = Path(__file__).parent / "fixtures" / "centene_page.html"
    links = parse_toc_links(page.read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for link in links:  # pre-populate C1's download cache so nothing is fetched
        shutil.copy(world["index"], raw / link.filename)
    out = tmp_path / "out"
    assert main(["run", "--run-c1", "--c1-html-file", str(page), "--out", str(out), "--no-probe"]) == 0
    assert (tmp_path / "data" / "index_table.csv").exists()
    s = json.loads((out / "c2_run_summary.json").read_text())
    assert s["c1"].endswith("index_table.csv") and s["index_sources"]
    assert s["in_scope_ppo_rows"] > 0
