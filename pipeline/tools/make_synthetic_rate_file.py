"""Write a large synthetic rate file shaped like Health Net's CA file, to measure C4-C7 speed.

  python -m tools.make_synthetic_rate_file out.json.gz --refs 40000 --codes 3000 --entries 300

Codes are taken from the HiLabs workbook first (--workbook), then made up. network_name is blank
on every reference, as in the real file. The file is written as a stream, so any size fits in memory.
"""
from __future__ import annotations

import argparse
import gzip
import json
import random


def npi(base9: int) -> int:
    s = str(base9)
    total = 0
    for i, ch in enumerate(reversed("80840" + s)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return int(s + str((10 - total % 10) % 10))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--refs", type=int, default=40000)
    ap.add_argument("--codes", type=int, default=3000)
    ap.add_argument("--entries", type=int, default=300, help="negotiated_rates entries per code")
    ap.add_argument("--refset", type=int, default=20, help="references per entry")
    ap.add_argument("--pool", type=int, default=4000,
                    help="distinct reference lists reused across entries (real files reuse contract lists); "
                         "0 = a fresh random list for every entry (worst case)")
    ap.add_argument("--workbook")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    codes = []
    if a.workbook:
        from ptd_c3.codes import load_expected_codes
        codes = sorted(load_expected_codes(a.workbook).codes)
    codes += [str(10000 + i) for i in range(a.codes - len(codes))]
    # 70% of references are PPO, 30% HMO; contract lists never mix them, and only HMO lists are capitated
    cut = int(a.refs * 0.7)
    ppo_ids, hmo_ids = range(1, cut + 1), range(cut + 1, a.refs + 1)
    pool = [rnd.sample(ppo_ids if i % 10 < 7 else hmo_ids, a.refset) for i in range(a.pool)]
    hmo_pool = [p for i, p in enumerate(pool) if i % 10 >= 7] or [rnd.sample(hmo_ids, a.refset)]
    opener = gzip.open if a.out.endswith(".gz") else open
    with opener(a.out, "wt", encoding="utf-8") as fh:
        fh.write('{"reporting_entity_name": "Synthetic", "reporting_entity_type": "TPA", '
                 '"last_updated_on": "2026-09-30", "version": "2.0.0", "provider_references": [')
        nxt = 200000000
        for g in range(1, a.refs + 1):
            k = rnd.choice([1, 1, 2, 3, 4, 6, 8, 12])
            npis = [npi(nxt + j) for j in range(k)]
            nxt += k
            fh.write(("," if g > 1 else "") + json.dumps({"provider_group_id": g, "network_name": [""],
                     "provider_groups": [{"npi": npis, "tin": {"type": "ein", "value": str(900000000 + g // 3)}}]}))
        fh.write('], "in_network": [')
        for ci, code in enumerate(codes):
            base = rnd.uniform(20, 900)
            nrs = []
            for _ in range(a.entries):
                if ci % 97 == 0:
                    refs = rnd.choice(hmo_pool)
                else:
                    refs = rnd.choice(pool) if pool else rnd.sample(range(1, a.refs + 1), a.refset)
                f = rnd.choice([0.7, 0.85, 1.0, 1.15, 1.3])
                prices = [{"negotiated_type": "negotiated", "negotiated_rate": round(base * f, 2),
                           "expiration_date": "9999-12-31", "billing_class": "professional", "service_code": ["11", "22"]}]
                if rnd.random() < 0.5:
                    prices.append({"negotiated_type": "negotiated", "negotiated_rate": round(base * f * 1.6, 2),
                                   "expiration_date": "9999-12-31", "billing_class": "institutional", "setting": "outpatient"})
                nrs.append({"provider_references": refs, "negotiated_prices": prices})
            fh.write(("," if ci else "") + json.dumps({
                "negotiation_arrangement": "capitation" if ci % 97 == 0 else "ffs", "name": code,
                "billing_code_type": "CPT" if code[0].isdigit() else "HCPCS", "billing_code_type_version": "2026",
                "billing_code": code, "description": f"code {code}", "negotiated_rates": nrs}))
        fh.write("]}")


if __name__ == "__main__":
    main()
