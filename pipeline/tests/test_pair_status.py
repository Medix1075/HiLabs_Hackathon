"""Network status per NPI x TIN (network.decide_pair) and the PPO-evidence tie-break (extract._pick)."""
from ptd_c4.extract import _pick
from ptd_c4.network import (CONFIRMED_NON_PPO, CONFIRMED_PPO, NON, PPO, PPO_CANDIDATE, UNRESOLVED, decide_pair)


class Dir:
    enabled = True

    def __init__(self, listings):  # {(npi, tin): (sides, kind)}
        self.listings = listings

    def match(self, npi, tin):
        return self.listings.get((npi, tin), (set(), ""))


def test_other_members_listings_do_not_confirm_an_unlisted_npi():
    refs = {"2613": (CONFIRMED_PPO, "direct: directory")}
    st, basis, flags = decide_pair(1, "11-1", refs, Dir({}))
    assert st == PPO_CANDIDATE and basis.startswith("inferred: other members of reference 2613") and "downgraded" in flags


def test_own_listing_confirms_even_when_references_are_shared():
    refs = {"a": (PPO_CANDIDATE, "inferred: directory_shared"), "b": (UNRESOLVED, "no network evidence")}
    st, basis, flags = decide_pair(1, "11-1", refs, Dir({(1, "11-1"): ({PPO}, "npi")}))
    assert st == CONFIRMED_PPO and basis.startswith("direct: own directory listing") and "own_listing" in flags


def test_listing_under_another_tin_is_not_enough():
    refs = {"a": (CONFIRMED_PPO, "direct: directory")}
    st, _, _ = decide_pair(1, "11-1", refs, Dir({(1, "11-1"): ({PPO}, "npi_tin_mismatch")}))
    assert st == PPO_CANDIDATE


def test_contract_level_evidence_still_confirms_every_member():
    refs = {"a": (CONFIRMED_PPO, "direct: network_name")}
    assert decide_pair(1, "11-1", refs, Dir({}))[0] == CONFIRMED_PPO


def test_own_listing_beats_capitation_and_is_flagged_dual_contract():
    refs = {"hmo": (CONFIRMED_NON_PPO, "direct: capitation"), "ppo": (UNRESOLVED, "no network evidence")}
    st, basis, flags = decide_pair(1, "11-1", refs, Dir({(1, "11-1"): ({PPO}, "npi")}))
    assert st == CONFIRMED_PPO and "dual_contract" in flags and "capitated" in basis
    # with no other contract to take a PPO rate from, the pair stays a candidate
    st, _, flags = decide_pair(1, "11-1", {"hmo": refs["hmo"]}, Dir({(1, "11-1"): ({PPO}, "npi")}))
    assert st == PPO_CANDIDATE and "dual_contract" in flags


def test_statuses_without_ppo_confirmation_are_unchanged():
    assert decide_pair(1, "t", {"a": (CONFIRMED_NON_PPO, "direct: capitation")}, Dir({}))[0] == CONFIRMED_NON_PPO
    assert decide_pair(1, "t", {"a": (UNRESOLVED, "no network evidence")}, None)[0] == UNRESOLVED
    assert decide_pair(1, "t", {"a": (PPO_CANDIDATE, "inferred: x")}, None)[0] == PPO_CANDIDATE
    assert decide_pair(1, "t", {"a": (UNRESOLVED, "")}, Dir({(1, "t"): ({NON}, "npi")}))[0] == CONFIRMED_NON_PPO


def test_tie_break_prefers_the_contract_with_more_ppo_evidence_before_the_lowest_rate():
    order = ["network", "ppo_evidence", "expiry", "support", "lowest"]
    # (rate, expiry, net_rank, tag, conf, support, status, ppo_share)
    hmo_like = (80.0, "9999-12-31", 21, "PPO", "low", 1, PPO_CANDIDATE, 0.1)
    ppo_like = (95.0, "9999-12-31", 21, "PPO", "low", 1, PPO_CANDIDATE, 0.6)
    win, rule = _pick([hmo_like, ppo_like], order)
    assert win[0] == 95.0 and rule == "ppo_evidence"
    # no difference in evidence: the old rule (lowest) still decides
    win, rule = _pick([hmo_like, (95.0, *hmo_like[1:])], order)
    assert win[0] == 80.0 and rule == "lowest"


def test_geocoder_merges_one_place_spelled_two_ways(tmp_path):
    from ptd_c5.address import standardize
    from ptd_c5.geocode import _qkey, _query, parse_batch, site_aliases

    a = standardize("4300 Long Beach Blvd", "", "Long Beach", "CA", "90807")
    b = standardize("4300 N. Long Beach Boulevard", "Suite 200", "Long Beach", "CA", "90807-1234")
    c = standardize("4310 Long Beach Blvd", "", "Long Beach", "CA", "90807")   # next door: another site
    assert a.site_key != b.site_key
    answers = {_qkey(_query(a)): "4300 N LONG BEACH BLVD, LONG BEACH, CA, 90807",
               _qkey(_query(b)): "4300 N LONG BEACH BLVD, LONG BEACH, CA, 90807",
               _qkey(_query(c)): "4310 N LONG BEACH BLVD, LONG BEACH, CA, 90807"}
    cfg = {"geocode": {"mode": "auto", "state": "CA", "cache_path": str(tmp_path / "geo.jsonl")}}
    fetch = lambda qs: {_qkey(q): {"matched": answers[_qkey(q)], "exact": True, "lon": 0.0, "lat": 0.0} for q in qs}
    alias, rep = site_aliases({a.site_key: a, b.site_key: b, c.site_key: c}, cfg, lambda *_: None, fetch)
    assert len(alias) == 1 and rep["merged_sites"] == 1
    merged = {alias.get(k, k) for k in (a.site_key, b.site_key)}
    assert len(merged) == 1 and c.site_key not in alias
    # the second run reads the cache and asks nothing
    alias2, rep2 = site_aliases({a.site_key: a, b.site_key: b, c.site_key: c}, cfg, lambda *_: None,
                                lambda qs: (_ for _ in ()).throw(AssertionError("asked again")))
    assert alias2 == alias and rep2["asked"] == 0
    # the batch answer format
    got = parse_batch('"0","4300 N Long Beach Blvd, Long Beach, CA, 90807","Match","Exact",'
                      '"4300 N LONG BEACH BLVD, LONG BEACH, CA, 90807","-118.18,33.83","142850101","R"\n'
                      '"1","1 Nowhere Rd, X, CA, 90000","No_Match"\n')
    assert got["0"]["matched"].startswith("4300 N LONG BEACH") and got["1"]["matched"] == ""
