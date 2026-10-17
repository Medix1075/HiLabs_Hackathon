"""Roll the benchmarked rates up into one cost-effectiveness score per provider (NPI + TIN) and per TIN.

For provider p with benchmarked codes c (only rates whose peer group reached min_peers):

  w_c        code weight: SPEC_PROC_WEIGHT of c in p's specialty, else c's weight across all
             specialties, else 1 (HiLabs workbook; see config/c7.yaml)
  L          sum_c w_c * ln(rate_c / peer median_c) / sum_c w_c   (weighted log index)
  n_eff      (sum w)^2 / sum w^2                                   (Kish effective number of codes)
  L*         L * n_eff / (n_eff + k)                                (shrunk towards the market)
  cost index exp(L*)                                               (0.85 = 15% below similar providers)
  score      100 * (1 - percentile of the cost index among similar providers)  (100 = cheapest)

Also reported: share of the code weight in the cheapest / most expensive quartile of its peer
group, coverage of the specialty's code volume, and a confidence level from n_eff and coverage.
A geometric mean is used because rate ratios are multiplicative: 0.5x and 2x average to 1x.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from .bench import factorize, group_stats


def code_weight_fn(sp, overall: dict, mode: str):
    def w(spec_cd: str, code: str) -> float:
        if mode != "workbook":
            return 1.0
        prof = sp.codes.get(spec_cd) if sp is not None else None
        if prof and code in prof:
            return prof[code]
        if code in overall:
            return overall[code]
        return min(overall.values()) if overall else 1.0
    return w


def tier(index: float, tiers: list[dict]) -> str:
    for t in tiers:
        if t["max_index"] is None or index <= t["max_index"]:
            return t["name"]
    return tiers[-1]["name"]


def provider_scores(rows: list[dict], cfg: dict, sp=None, overall_weights: dict | None = None) -> list[dict]:
    pc = cfg["provider_score"]
    wfn = code_weight_fn(sp, overall_weights or {}, pc["weighting"])
    acc: dict[tuple, dict] = {}
    for r in rows:
        if not r.get("peer_ok") or not r.get("rate_index"):
            continue
        k = (r["npi"], r["tin"])
        a = acc.get(k)
        if a is None:
            a = acc[k] = {"npi": r["npi"], "tin": r["tin"], "npi_type": r["npi_type"], "specialty": r["specialty"],
                          "spec_cd": r["spec_cd"], "region": r["region"], "sw": 0.0, "sw2": 0.0, "swl": 0.0,
                          "q1": 0.0, "q4": 0.0, "codes": set(), "levels": defaultdict(int)}
        w = wfn(r["spec_cd"], r["code"])
        a["sw"] += w
        a["sw2"] += w * w
        a["swl"] += w * math.log(r["rate_index"])
        a["q1"] += w * (r["quartile"] == 1)
        a["q4"] += w * (r["quartile"] == 4)
        a["codes"].add(r["code"])
        a["levels"][r["peer_level"]] += 1
    k_shrink = pc["shrinkage_k"]
    out = []
    for a in acc.values():
        if a["sw"] <= 0:
            continue
        L = a["swl"] / a["sw"]
        n_eff = a["sw"] ** 2 / a["sw2"]
        Ls = L * n_eff / (n_eff + k_shrink)
        prof = sp.codes.get(a["spec_cd"]) if sp is not None and a["spec_cd"] else None
        coverage = (sum(v for c, v in prof.items() if c in a["codes"]) / sum(prof.values())) if prof else None
        conf = "low"
        for level in ("high", "medium"):
            c = pc["confidence"][level]
            if n_eff >= c["min_n_eff"] and (coverage is None or coverage >= c["min_coverage"]):
                conf = level
                break
        out.append({
            "npi": a["npi"], "tin": a["tin"], "npi_type": a["npi_type"], "specialty": a["specialty"],
            "spec_cd": a["spec_cd"], "region": a["region"], "codes_benchmarked": len(a["codes"]),
            "n_eff": round(n_eff, 2), "raw_cost_index": round(math.exp(L), 4), "cost_index": round(math.exp(Ls), 4),
            "share_cheapest_quartile": round(a["q1"] / a["sw"], 4),
            "share_most_expensive_quartile": round(a["q4"] / a["sw"], 4),
            "specialty_code_coverage": None if coverage is None else round(coverage, 4),
            "tier": tier(math.exp(Ls), cfg["tiers"]), "score_confidence": conf,
            "peer_levels_used": "; ".join(f"{k} {v}" for k, v in sorted(a["levels"].items())),
            "_ls": Ls,
        })
    _cohort_rank(out, pc["cohort_levels"], pc["min_cohort"])
    for r in out:
        r.pop("_ls")
    return out


def _cohort_rank(out: list[dict], levels: list[list[str]], min_cohort: int) -> None:
    """Score = 100 x (1 - percentile of the shrunk index) within the most specific cohort that is big enough."""
    if not out:
        return
    keys_by_level = [[tuple(r[k] for k in lv) for r in out] for lv in levels]
    chosen = [None] * len(out)
    for li, keys in enumerate(keys_by_level):
        ids, _ = factorize(keys)
        size = np.bincount(ids)[ids]
        for i in range(len(out)):
            if chosen[i] is None and (size[i] >= min_cohort or li == len(levels) - 1):
                chosen[i] = (li, keys[i])
    gid, uniq = factorize(chosen)
    vals = np.array([r["_ls"] for r in out])
    size, _, _, _, pct = group_stats(gid, vals, len(uniq))
    rank_order = np.lexsort((vals, gid))
    rank = np.empty(len(out), dtype=np.int64)
    counts = np.bincount(gid)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    rank[rank_order] = np.arange(len(out)) - starts[gid[rank_order]] + 1
    for i, r in enumerate(out):
        li, key = chosen[i]
        r["cost_score"] = round(100 * (1 - pct[i]), 1)
        r["cohort"] = " / ".join(str(k) for k in key) if key else "all providers"
        r["cohort_size"] = int(size[i])
        r["rank_in_cohort"] = int(rank[i])


def tin_scores(providers: list[dict], cfg: dict) -> list[dict]:
    """A TIN's index: its members' shrunk log indexes, weighted by each member's n_eff."""
    acc = defaultdict(lambda: {"w": 0.0, "wl": 0.0, "members": 0, "specialties": set(), "regions": set()})
    for p in providers:
        a = acc[p["tin"]]
        a["w"] += p["n_eff"]
        a["wl"] += p["n_eff"] * math.log(p["cost_index"])
        a["members"] += 1
        a["specialties"].add(p["specialty"])
        a["regions"].add(p["region"])
    out = []
    for tin, a in acc.items():
        idx = math.exp(a["wl"] / a["w"]) if a["w"] else 1.0
        out.append({"tin": tin, "members_scored": a["members"], "cost_index": round(idx, 4),
                    "tier": tier(idx, cfg["tiers"]), "specialties": "; ".join(sorted(map(str, a["specialties"])))[:300],
                    "regions": "; ".join(sorted(map(str, a["regions"])))[:300]})
    if out:
        vals = np.array([math.log(r["cost_index"]) for r in out])
        _, _, _, _, pct = group_stats(np.zeros(len(out), dtype=np.int64), vals, 1)
        for r, p in zip(out, pct):
            r["cost_score"] = round(100 * (1 - p), 1)
    return sorted(out, key=lambda r: r["cost_index"])
