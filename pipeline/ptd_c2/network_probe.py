"""Step 4 (+ Step 5 payment sample): read network_name from the start of each rate file.

One streaming pass with ijson over the decompressed bytes:
  * provider_references[].network_name (string or list) -> network names per reference
  * provider_references[].provider_groups[].tin / npi     -> TIN spread (Step 5)
  * provider_references[].location                        -> remote reference files, fetched if needed
  * in_network[].negotiation_arrangement                  -> ffs / capitation share (Step 5)
It stops as soon as it has enough, so multi-GB files are never downloaded in full.
If in_network comes before provider_references in a file, the byte cap decides how far we go
and the result says so.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import ijson

from .stream import ByteCapReached, open_stream

PR = "provider_references.item"
IN = "in_network.item"


def label_network_name(name: str, cfg: dict) -> str:
    for label, rx in cfg["network_probe"]["_label_rules"]:
        if rx.search(name):
            return label
    return "UNKNOWN"


def _new_state() -> dict:
    return {
        "names": Counter(), "tins": set(), "npis_per_tin": Counter(), "locations": [],
        "arrangements": Counter(), "refs": 0, "in_items": 0, "refs_complete": False, "in_complete": False,
        "empty_names": 0,
        "top": {}, "stop_reason": "end_of_file",
    }


def _scan(fh, pc: dict, st: dict) -> None:
    """Single pass over the event stream, filling `st` as it goes so a byte-cap stop keeps partial results."""
    cur_tin, cur_npis = None, 0
    for prefix, event, value in ijson.parse(fh, use_float=True):
        if prefix in ("reporting_entity_name", "reporting_entity_type", "last_updated_on", "version") \
                and event in ("string", "number"):
            st["top"][prefix] = value
        elif prefix.startswith(PR):
            if prefix == PR and event == "end_map":
                st["refs"] += 1
            elif prefix in (PR + ".network_name", PR + ".network_name.item") and event == "string":
                if value.strip():
                    st["names"][value.strip()] += 1
                else:  # seen in Health Net's CA file: network_name present but blank
                    st["empty_names"] += 1
            elif prefix == PR + ".location" and event == "string":
                if len(st["locations"]) < 1000:
                    st["locations"].append(value)
            elif prefix == PR + ".provider_groups.item.tin.value":
                cur_tin = str(value)
                st["tins"].add(cur_tin)
            elif prefix == PR + ".provider_groups.item.npi.item":
                cur_npis += 1
            elif prefix == PR + ".provider_groups.item" and event == "end_map":
                if cur_tin is not None:
                    st["npis_per_tin"][cur_tin] += cur_npis
                cur_tin, cur_npis = None, 0
        elif prefix == "provider_references" and event == "end_array":
            st["refs_complete"] = True
        elif prefix.startswith(IN):
            if prefix == IN and event == "end_map":
                st["in_items"] += 1
            elif prefix == IN + ".negotiation_arrangement" and event == "string":
                st["arrangements"][value.lower()] += 1
            elif prefix == IN + ".negotiated_rates.item.provider_groups.item.tin.value":
                st["tins"].add(str(value))
        elif prefix == "in_network" and event == "end_array":
            st["in_complete"] = True
        have_refs = st["refs_complete"] or st["refs"] >= pc["max_provider_references"]
        have_items = st["in_complete"] or st["in_items"] >= pc["max_in_network_items"]
        if have_refs and have_items:
            st["stop_reason"] = "collected_enough"
            return


def _scan_location(url: str, pc: dict) -> dict:
    """Remote provider reference file: {provider_groups:[...]} and possibly network_name."""
    names, tins = Counter(), set()
    with open_stream(url, max_bytes=pc["max_bytes"], timeout=pc["timeout_seconds"], retries=pc["retries"],
                     user_agent=pc["user_agent"]) as (fh, _):
        for prefix, event, value in ijson.parse(fh):
            if prefix.endswith("network_name") or prefix.endswith("network_name.item"):
                if event == "string" and value.strip():
                    names[value.strip()] += 1
            elif prefix.endswith("tin.value"):
                tins.add(str(value))
    return {"names": names, "tins": tins}


def probe_file(url: str, cfg: dict) -> dict:
    pc = cfg["network_probe"]
    t0 = time.time()
    result = {"rate_file_url": url, "status": "", "error": "", "bytes_read": 0}
    counter = None
    scan = _new_state()
    try:
        with open_stream(url, max_bytes=pc["max_bytes"], timeout=pc["timeout_seconds"], retries=pc["retries"],
                         user_agent=pc["user_agent"]) as (fh, counter):
            try:
                _scan(fh, pc, scan)
            except ByteCapReached:
                scan["stop_reason"] = "byte_cap_reached"
            result["bytes_read"] = counter.bytes_read
    except Exception as exc:
        result.update(status="error", error=f"{exc.__class__.__name__}: {exc}"[:500],
                      bytes_read=counter.bytes_read if counter else 0, seconds=round(time.time() - t0, 2))
        if not scan["names"]:
            return _finalise(result, None, cfg)
        scan["stop_reason"] = "error_after_partial_read"

    loc_fetched = 0
    if scan["locations"] and not scan["names"]:
        for loc in scan["locations"][: pc["max_location_fetches"]]:
            try:
                sub = _scan_location(loc, pc)
                scan["names"].update(sub["names"])
                scan["tins"] |= sub["tins"]
                loc_fetched += 1
            except Exception as exc:
                result["error"] = f"location fetch failed: {exc.__class__.__name__}: {exc}"[:500]
    result["location_refs"] = len(scan["locations"])
    result["location_files_fetched"] = loc_fetched
    result["seconds"] = round(time.time() - t0, 2)
    if not result["status"]:
        result["status"] = ("ok" if scan["names"] else
                            "network_name_blank" if scan["empty_names"] else
                            "byte_cap_reached" if scan["stop_reason"] == "byte_cap_reached" else
                            "no_network_name_found")
    return _finalise(result, scan, cfg)


def _finalise(result: dict, scan: dict | None, cfg: dict) -> dict:
    pc = cfg["network_probe"]
    if scan is None:
        result.update(network_names={}, network_labels={}, network_label="UNCONFIRMED", ppo_network_names=[],
                      provider_references_read=0, provider_references_complete=False, in_network_items_read=0,
                      arrangements={}, distinct_tins=0, top_level={}, stop_reason="")
        return result
    labels = Counter()
    per_name = {}
    for name, n in scan["names"].items():
        lab = label_network_name(name, cfg)
        per_name[name] = lab
        labels[lab] += n
    ppo_labels = {"PPO_FULL", "PPO_ENHANCEDCARE"}
    found = set(labels)
    if not found:
        overall = "UNCONFIRMED"
    elif found <= ppo_labels:
        overall = "PPO_ENHANCEDCARE" if found == {"PPO_ENHANCEDCARE"} else "PPO_FULL" if found == {"PPO_FULL"} else "PPO_MIXED_VARIANTS"
    elif found & ppo_labels:
        overall = "MIXED"
    elif found == {"UNKNOWN"}:
        overall = "UNKNOWN_NETWORK"
    else:
        known = found - {"UNKNOWN"}
        overall = next(iter(known)) if len(known) == 1 else "NON_PPO_MIXED"
    big_groups = sum(1 for v in scan["npis_per_tin"].values() if v >= 50)
    result.update(
        network_names=dict(scan["names"]), network_name_labels=per_name, network_labels=dict(labels),
        network_label=overall,
        ppo_network_names=sorted(n for n, lab in per_name.items() if lab in ppo_labels),
        provider_references_read=scan["refs"], provider_references_complete=scan["refs_complete"],
        in_network_items_read=scan["in_items"], arrangements=dict(scan["arrangements"]),
        distinct_tins=len(scan["tins"]), tins_with_50plus_npis=big_groups, blank_network_names=scan["empty_names"],
        top_level=scan["top"], stop_reason=scan["stop_reason"],
        sampled_partially=not scan["refs_complete"],
    )
    if result["sampled_partially"] and overall not in ("UNCONFIRMED",):
        result["note"] = (f"read {scan['refs']} provider references before stopping; "
                          "names beyond the sample were not seen")
    return result


def probe_files(urls: list[str], cfg: dict, cache_path: Path | None = None, log=print) -> dict[str, dict]:
    """Probe many files in parallel, with an on-disk cache keyed by URL so reruns are cheap."""
    cache = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text())
    todo = [u for u in urls if u not in cache or cache[u].get("status") == "error"]
    results = {u: cache[u] for u in urls if u in cache and u not in todo}
    if todo:
        log(f"[step4] probing {len(todo)} rate file(s) with {cfg['network_probe']['workers']} worker(s)"
            f" ({len(results)} from cache)")
    with ThreadPoolExecutor(max_workers=max(1, cfg["network_probe"]["workers"])) as pool:
        futs = {pool.submit(probe_file, u, cfg): u for u in todo}
        for fut in as_completed(futs):
            res = fut.result()
            results[res["rate_file_url"]] = res
            log(f"[step4]   {res['status']:<22} {res['network_label']:<18} {res['rate_file_url']}")
            if cache_path:
                cache[res["rate_file_url"]] = res
                cache_path.write_text(json.dumps(cache, indent=1, default=list))
    return results
