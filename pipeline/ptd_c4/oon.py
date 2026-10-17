"""Read the providers out of an allowed-amount (out-of-network) file.

An allowed-amount file lists, per billing code, what a plan allowed for claims from providers who
were OUT of its network (CMS schema: out_of_network[].allowed_amounts[] with a tin, then
payments[].providers[] with billed_charge and npi[]). So an NPI found here was paid out of network
by at least one plan that lists the file:

  file listed only by PPO plans   -> the provider was outside the PPO network: non-PPO evidence
  file shared with HMO/EOA plans  -> out of network for *some* plan: a note, not evidence

C2 says which case applies (c2_allowed_amount_files.csv, column ppo_only).
"""
from __future__ import annotations

import re
from collections import Counter

import ijson

from ptd_c2.stream import open_stream

from .ingest import cache_file


def read_oon_providers(url: str, sc: dict, log=print) -> tuple[Counter, dict]:
    """Return (Counter of (npi, tin) -> number of out_of_network items, stats)."""
    path = cache_file(url, sc, log)
    seen: Counter = Counter()
    items = 0
    with open_stream(path, timeout=sc["timeout_seconds"], retries=sc["retries"], user_agent=sc["user_agent"]) as (fh, _):
        for item in ijson.items(fh, "out_of_network.item"):
            items += 1
            here = set()
            for aa in item.get("allowed_amounts") or []:
                tin = re.sub(r"\D", "", str((aa.get("tin") or {}).get("value") or ""))
                for pay in aa.get("payments") or []:
                    for prov in pay.get("providers") or []:
                        for npi in prov.get("npi") or []:
                            n = re.sub(r"\D", "", str(npi))
                            if len(n) == 10:
                                here.add((int(n), tin))
            for k in here:
                seen[k] += 1
    return seen, {"items": items, "providers": len(seen)}


def store_oon(con, file_id: int, url: str, ppo_only: bool, seen: Counter) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS c4_oon_providers (file_id INTEGER, oon_url TEXT, ppo_only INTEGER, "
                "npi INTEGER, tin TEXT, items INTEGER)")
    con.executemany("INSERT INTO c4_oon_providers VALUES (?,?,?,?,?,?)",
                    [(file_id, url, int(ppo_only), npi, tin, k) for (npi, tin), k in seen.items()])
    con.commit()
