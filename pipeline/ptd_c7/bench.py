"""Benchmark every rate against its peer group, vectorised with numpy.

For each rate row the peer group is the set of providers with the same code and price variant,
narrowed by the first peer level (specialty, region, NPI type...) that still has min_peers
providers. Within the group (all sorted once with one lexsort):

  peer median, Q1, Q3   linear interpolation, like numpy.quantile
  rate index            rate / peer median           (1.00 = what similar providers are paid)
  percentile            mid-rank / (n - 1)           (0 = cheapest in the group, 1 = most expensive)
  quartile              1..4 from the percentile
"""
from __future__ import annotations

import numpy as np


def factorize(keys: list[tuple]) -> tuple[np.ndarray, list[tuple]]:
    index: dict[tuple, int] = {}
    ids = np.fromiter((index.setdefault(k, len(index)) for k in keys), dtype=np.int64, count=len(keys))
    return ids, list(index)


def group_stats(gid: np.ndarray, values: np.ndarray, n_groups: int):
    """Per row: group size, Q1, median, Q3 of the group, and the row's mid-rank percentile."""
    order = np.lexsort((values, gid))
    g, v = gid[order], values[order]
    counts = np.bincount(g, minlength=n_groups)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])

    def quantile(q):
        pos = starts + q * (counts - 1).clip(min=0)
        lo, hi = np.floor(pos).astype(np.int64), np.ceil(pos).astype(np.int64)
        lo, hi = lo.clip(0, len(v) - 1), hi.clip(0, len(v) - 1)
        frac = pos - np.floor(pos)
        out = v[lo] * (1 - frac) + v[hi] * frac
        return np.where(counts > 0, out, np.nan)

    q1, med, q3 = quantile(0.25), quantile(0.5), quantile(0.75)
    # mid-rank of ties: runs of equal (group, value)
    new_run = np.ones(len(v), dtype=bool)
    new_run[1:] = (g[1:] != g[:-1]) | (v[1:] != v[:-1])
    run_id = np.cumsum(new_run) - 1
    run_start = np.flatnonzero(new_run)
    run_len = np.bincount(run_id)
    mid = run_start[run_id] + (run_len[run_id] - 1) / 2.0
    n = counts[g]
    pct_sorted = np.where(n > 1, (mid - starts[g]) / np.maximum(n - 1, 1), 0.5)
    pct = np.empty(len(v))
    pct[order] = pct_sorted
    return counts[gid], q1[gid], med[gid], q3[gid], pct


def assign_levels(level_keys: list[list[tuple]], min_peers: int):
    """Pick, per row, the first level whose group has at least min_peers rows. Returns (level, group id)."""
    n = len(level_keys[0]) if level_keys else 0
    level = np.full(n, -1, dtype=np.int64)
    final_keys: list = [None] * n
    for li, keys in enumerate(level_keys):
        ids, _ = factorize(keys)
        size = np.bincount(ids)[ids]
        take = (level < 0) & ((size >= min_peers) | (li == len(level_keys) - 1))
        level[take] = li
        for i in np.flatnonzero(take):
            final_keys[i] = (li, keys[i])
    gid, uniq = factorize(final_keys)
    return level, gid, len(uniq)


def benchmark(rows: list[dict], cfg: dict) -> list[dict]:
    """Adds peer_level, peer_n, peer_q1, peer_median, peer_q3, rate_index, percentile, quartile to each row."""
    if not rows:
        return rows
    variant = cfg["variant"]
    levels = cfg["peer_levels"]
    base = [tuple([r["code_type"], r["code"]] + [r[k] for k in variant]) for r in rows]
    level_keys = [[b + tuple(r[k] for k in lv["keys"]) for b, r in zip(base, rows)] for lv in levels]
    level, gid, n_groups = assign_levels(level_keys, cfg["min_peers"])
    rates = np.array([r["rate"] for r in rows], dtype=float)
    size, q1, med, q3, pct = group_stats(gid, rates, n_groups)
    for i, r in enumerate(rows):
        r["peer_level"] = levels[level[i]]["name"]
        r["peer_n"] = int(size[i])
        r["peer_q1"], r["peer_median"], r["peer_q3"] = round(float(q1[i]), 4), round(float(med[i]), 4), round(float(q3[i]), 4)
        r["rate_index"] = round(rates[i] / med[i], 4) if med[i] > 0 else None
        r["percentile"] = round(float(pct[i]), 4)
        r["quartile"] = 1 + min(3, int(pct[i] * 4)) if size[i] > 1 else None
        r["peer_ok"] = size[i] >= cfg["min_peers"]
    return rows
