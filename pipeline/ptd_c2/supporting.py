"""Step 5: supporting evidence. Raises or lowers confidence; never overrides network_name.

* oon_signal: does the plan's allowed-amount (out-of-network) file hold real claim records?
  (Hypothesis) records -> PPO/POS (members can go out of network); empty/missing -> HMO/EPO.
* payment_signal: share of ffs vs capitation and the TIN spread in the sampled in_network items.
  (Hypothesis) mostly ffs across many TINs -> PPO; meaningful capitation -> HMO.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import ijson

from .stream import ByteCapReached, open_stream

PPO_LEAN, HMO_LEAN, NEUTRAL, UNKNOWN = "PPO-leaning", "HMO-leaning", "neutral", "unknown"


def probe_oon_file(url: str, cfg: dict) -> dict:
    pc, sc = cfg["network_probe"], cfg["supporting"]
    out = {"oon_file_url": url, "status": "", "records_seen": 0, "bytes_read": 0, "error": ""}
    t0 = time.time()
    counter = None
    try:
        with open_stream(url, max_bytes=sc["oon_max_bytes"], timeout=pc["timeout_seconds"], retries=pc["retries"],
                         user_agent=pc["user_agent"]) as (fh, counter):
            try:
                for prefix, event, _ in ijson.parse(fh):
                    if prefix == "out_of_network.item" and event == "end_map":
                        out["records_seen"] += 1
                        if out["records_seen"] >= max(sc["oon_min_records_for_present"], 1):
                            break
            except ByteCapReached:
                pass
            out["bytes_read"] = counter.bytes_read
        out["status"] = "has_records" if out["records_seen"] >= sc["oon_min_records_for_present"] else "empty"
    except Exception as exc:
        out.update(status="error", error=f"{exc.__class__.__name__}: {exc}"[:300])
    out["seconds"] = round(time.time() - t0, 2)
    return out


def probe_oon_files(urls: list[str], cfg: dict, log=print) -> dict[str, dict]:
    urls = sorted({u for u in urls if u})
    if not urls:
        return {}
    log(f"[step5] probing {len(urls)} out-of-network file(s)")
    with ThreadPoolExecutor(max_workers=max(1, cfg["network_probe"]["workers"])) as pool:
        return {r["oon_file_url"]: r for r in pool.map(lambda u: probe_oon_file(u, cfg), urls)}


def oon_signal(oon_urls: set[str], oon_results: dict, enabled: bool) -> tuple[str, str]:
    """Returns (signal, measured value text) for one plan row."""
    urls = sorted(u for u in oon_urls if u)
    if not oon_urls:
        return UNKNOWN, "plan not matched in raw index (or no index given)"
    if not urls:
        return HMO_LEAN, "index lists no allowed_amount_file for this plan"
    if not enabled:
        return UNKNOWN, f"allowed_amount_file listed ({len(urls)}), not probed"
    statuses = [oon_results.get(u, {}).get("status", "not_probed") for u in urls]
    if "has_records" in statuses:
        return PPO_LEAN, "allowed_amount_file has out_of_network records"
    if all(s == "empty" for s in statuses):
        return HMO_LEAN, "allowed_amount_file has no out_of_network records"
    return UNKNOWN, f"allowed_amount_file status: {','.join(statuses)}"


def payment_signal(probe: dict | None, cfg: dict) -> tuple[str, str]:
    pay = cfg["supporting"]["payment"]
    if not probe or probe.get("status") == "error":
        return UNKNOWN, "rate file not probed"
    arr = probe.get("arrangements") or {}
    n = sum(arr.values())
    tins = probe.get("distinct_tins", 0)
    if n < pay["min_items"]:
        return UNKNOWN, f"only {n} in_network items sampled; {tins} distinct TINs"
    ffs = arr.get("ffs", 0) / n
    cap = arr.get("capitation", 0) / n
    measured = f"ffs {ffs:.0%}, capitation {cap:.0%} of {n} items; {tins} distinct TINs"
    if cap >= pay["capitation_share_hmo"]:
        return HMO_LEAN, measured
    if ffs >= pay["ffs_share_ppo"] and tins >= pay["min_distinct_tins_ppo"]:
        return PPO_LEAN, measured
    return NEUTRAL, measured
