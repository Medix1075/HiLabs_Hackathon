"""Give every provider reference in a rate file a network status.

Why this exists: C2 found that all of Health Net's California group plans share one rate file
whose network_name is blank, so PPO and HMO providers sit in the same file with nothing on the
reference saying which is which. A provider can also be in both networks under two different
references, with two different prices, so the status belongs to the reference, not to the NPI.

Evidence (weights in config/c4.yaml), graded by what it can prove:
  network_name   (direct) the reference's own network_name, mapped with C2's network-name rules
  file_label     (direct) C2 says the whole file is a single PPO network (no mixing)
  directory      (direct) an NPI listed in a PPO / non-PPO network, when the listing can only be
                 about this reference; TIN-only, name + ZIP, shared or generic matches are weak
  capitation     (direct, non-PPO) paid per member per month for some services: an HMO arrangement
  oon_ppo_plan   (direct, non-PPO) paid out of network by a PPO plan (allowed-amount file)
  dual_contract  (inferred) one TIN in two references with different prices: the higher-paid one is PPO
  propagation    (inferred) label spreading over references that are listed together for the same price

The evidence is weighed into a tag (PPO / NON_PPO / UNKNOWN), then network_status() turns tag plus
evidence grades into CONFIRMED_PPO / PPO_CANDIDATE / UNRESOLVED / CONFIRMED_NON_PPO: only direct
evidence confirms. The two hypotheses are measured against references whose network is already known
and are used only when they pass (precision / held-out accuracy in the config), the same rule C2 uses
for its plan-name hypotheses. Every decision keeps its evidence string.
"""
from __future__ import annotations

import random
import sqlite3
import statistics
from collections import Counter, defaultdict

import numpy as np

from ptd_c2.network_probe import label_network_name

PPO, NON, UNKNOWN = "PPO", "NON_PPO", "UNKNOWN"
DOLLAR_TYPES = ("negotiated", "fee schedule", "derived")


class Evidence:
    def __init__(self, ref_ids: list[str]):
        self.ref_ids = ref_ids
        self.index = {r: i for i, r in enumerate(ref_ids)}
        self.ppo = np.zeros(len(ref_ids))
        self.non = np.zeros(len(ref_ids))
        self.notes: dict[int, list[str]] = defaultdict(list)
        self.sources: dict[int, set] = defaultdict(set)
        self.flags: dict[int, set] = defaultdict(set)     # notes that are not evidence for a side
        self.ppo_by: dict[int, dict] = defaultdict(dict)  # source -> weight, per side
        self.non_by: dict[int, dict] = defaultdict(dict)

    def add(self, i: int, side: str, weight: float, source: str, note: str) -> None:
        if weight <= 0:
            return
        if side == PPO:
            self.ppo[i] += weight
        else:
            self.non[i] += weight
        by = self.ppo_by if side == PPO else self.non_by
        by[i][source] = by[i].get(source, 0.0) + weight
        self.notes[i].append(f"{source}:{side}({weight:.2f}) {note}".strip())
        self.sources[i].add(source)

    def seeded_label(self, i: int, min_share: float) -> str | None:
        """The side a reference is known to be on, for testing and spreading hypotheses. Weak
        directory matches (TIN-only, NPI under another TIN) are left out: they are not proof."""
        p = self.ppo[i] - self.ppo_by[i].get("directory_weak", 0.0)
        n = self.non[i] - self.non_by[i].get("directory_weak", 0.0)
        if p + n == 0:
            return None
        if p / (p + n) >= min_share:
            return PPO
        if n / (p + n) >= min_share:
            return NON
        return None


# ------------------------------------------------------------------ seeds
def _seed_network_names(ev: Evidence, names: dict[str, str], cfg: dict, c2cfg: dict) -> int:
    ppo_labels, w = set(cfg["network"]["ppo_labels"]), cfg["network"]["seed_weights"]["network_name"]
    n = 0
    for ref, name in names.items():
        if not name:
            continue
        labels = {label_network_name(part, c2cfg) for part in name.split("|")}
        labels.discard(None)
        labels.discard("UNKNOWN")
        if not labels:
            continue
        n += 1
        side = PPO if labels & ppo_labels else NON
        if labels & ppo_labels and labels - ppo_labels:  # names for both sides on one reference
            ev.add(ev.index[ref], PPO, w / 2, "network_name", f"'{name}' (mixed)")
            ev.add(ev.index[ref], NON, w / 2, "network_name", f"'{name}' (mixed)")
        else:
            ev.add(ev.index[ref], side, w, "network_name", f"'{name}'")
    return n


def _seed_directory(ev: Evidence, con, file_id: int, directory, cfg: dict) -> tuple[int, list]:
    """Directory evidence. A directory lists an NPI in a network, not a reference, so:

    - an NPI that sits in one reference of this file is direct evidence for that reference
      (source "directory": the listing can only be about that contract);
    - an NPI in several references (e.g. a PPO and an HMO contract under one TIN) only says that
      at least one of them is in that network. That evidence is held back as "shared" and given
      out later by exclusion (see _allocate_shared);
    - a listing that matches only by TIN, lists the NPI under a different TIN, or was linked to the
      NPI by name + ZIP only, does not pin the provider to this contract: weak evidence
      ("directory_weak"), a candidate at most;
    - a listing under a generic payer name (no network named) is only noted ("directory_generic").
    """
    if not directory or not directory.enabled:
        return 0, []
    w = cfg["network"]["seed_weights"]["directory"]
    w_weak = w * cfg["network"].get("directory_weak_factor", 0.4)
    refs_of: dict[int, set] = defaultdict(set)
    members: Counter = Counter()
    for ref, npi, tin in con.execute("SELECT ref_id, npi, tin FROM c4_ref_members WHERE file_id = ?", (file_id,)):
        members[ref] += 1
        if npi is not None:
            refs_of[npi].add((ref, tin))
    unique: dict[str, list] = defaultdict(lambda: [0, 0])
    weak: dict[tuple, Counter] = defaultdict(Counter)
    shared: Counter = Counter()
    for npi, ref_tins in refs_of.items():
        strong: dict[str, set] = defaultdict(set)   # side -> refs whose (NPI, TIN) the listing names
        for ref, tin in ref_tins:
            sides, kind = directory.match(npi, tin)
            for side in sides:
                if kind == "npi":
                    strong[side].add(ref)
                else:
                    weak[(ref, side)][kind] += 1
            if npi in directory.generic_npis and ref in ev.index:
                ev.flags[ev.index[ref]].add("directory_generic")
        all_refs = {r for r, _ in ref_tins}
        for side, refs in strong.items():
            if len(all_refs) == 1:
                unique[next(iter(refs))][side == NON] += 1
            else:
                shared[(side, frozenset(all_refs))] += 1
    n = 0
    for ref, (hp, hn) in unique.items():
        if ref not in ev.index:
            continue
        n += 1
        i, m = ev.index[ref], members[ref]
        if hp:
            ev.add(i, PPO, w * hp / m, "directory", f"{hp}/{m} members listed in a PPO network (their only reference)")
        if hn:
            ev.add(i, NON, w * hn / m, "directory", f"{hn}/{m} members listed in a non-PPO network (their only reference)")
    for (ref, side), kinds in weak.items():
        if ref not in ev.index:
            continue
        k = sum(kinds.values())
        what = {"tin": "TIN only", "npi_tin_mismatch": "NPI under another TIN", "name_zip": "name + ZIP, no NPI shown"}
        why = ", ".join(f"{v} by {what.get(kk, kk)}" for kk, v in kinds.items())
        ev.add(ev.index[ref], side, w_weak * k / members[ref], "directory_weak",
               f"{k}/{members[ref]} members matched a {'PPO' if side == PPO else 'non-PPO'} listing weakly ({why})")
    pending = [{"side": side, "refs": sorted(refs), "npis": k, "w": w, "members": members, "done": False}
               for (side, refs), k in shared.items()]
    return n, pending


def _allocate_shared(ev: Evidence, pending: list, min_share: float, final: bool) -> int:
    """Give shared directory evidence to the references it can belong to.

    By exclusion (always): if some of the NPI's references are already known to be on the other
    side, the listing must be about the rest, so they get the full evidence.
    Final pass: what is still unresolved is split evenly over the references not excluded, as
    weak evidence ("directory_shared"), which never counts as high confidence on its own.
    """
    n = 0
    for p in pending:
        if p["done"]:
            continue
        other = NON if p["side"] == PPO else PPO
        refs = [r for r in p["refs"] if r in ev.index]
        labels = {r: ev.seeded_label(ev.index[r], min_share) for r in refs}
        excluded = [r for r in refs if labels[r] == other]
        rest = [r for r in refs if labels[r] != other]
        if not rest or (not excluded and not final):
            continue
        source = "directory" if excluded else "directory_shared"
        for r in rest:
            k = p["npis"] / p["members"][r]
            weight = p["w"] * k * (1.0 if excluded else 1.0 / len(rest))
            why = (f"{p['npis']} members listed in a {'PPO' if p['side'] == PPO else 'non-PPO'} network; "
                   + (f"their other reference(s) {', '.join(excluded)} are {other}" if excluded
                      else f"shared by references {', '.join(refs)}, split evenly"))
            ev.add(ev.index[r], p["side"], weight, source, why)
        p["done"] = True
        n += 1
    return n


def _seed_capitation(ev: Evidence, con, file_id: int, cfg: dict) -> int:
    nc = cfg["network"]
    w, min_share = nc["seed_weights"]["capitation"], nc["capitation_min_share"]
    n = 0
    # Two steps, so only references that have any capitation are aggregated.
    con.execute("DROP TABLE IF EXISTS temp.cap_refs")
    con.execute("CREATE TEMP TABLE cap_refs AS SELECT DISTINCT m.ref_id FROM c4_refset_stats s "
                "CROSS JOIN c4_refset_members m ON m.file_id = s.file_id AND m.refset_id = s.refset_id "
                "WHERE s.file_id = ? AND s.capitation_rows > 0", (file_id,))
    q = ("SELECT m.ref_id, SUM(s.rows), SUM(s.capitation_rows) FROM temp.cap_refs c "
         "CROSS JOIN c4_refset_members m ON m.file_id = ? AND m.ref_id = c.ref_id "
         "CROSS JOIN c4_refset_stats s ON s.file_id = m.file_id AND s.refset_id = m.refset_id GROUP BY m.ref_id")
    for ref, rows, cap in con.execute(q, (file_id,)):
        share = cap / rows if rows else 0
        if ref in ev.index and share >= min_share:
            n += 1
            ev.add(ev.index[ref], NON, w * min(1.0, share / 0.10 + 0.5), "capitation",
                   f"{cap:,} of {rows:,} price rows capitated")
    return n


def _seed_oon(ev: Evidence, con, file_id: int, cfg: dict) -> int:
    """Providers paid out of network (allowed-amount files, see oon.py).

    A file listed only by PPO plans: those NPIs were outside the PPO network, so their references
    (matched on NPI, and on TIN when the file gives one) get non-PPO evidence. A shared file only adds
    a note, since the out-of-network plan may have been an HMO.
    """
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'c4_oon_providers'").fetchone():
        return 0
    w = cfg["network"].get("oon", {}).get("weight", 0.8)
    hits: dict[str, list] = defaultdict(lambda: [0, 0, 0])   # ref -> [members hit (ppo-only), shared hits, members]
    members: Counter = Counter()
    q = ("SELECT m.ref_id, m.npi, MAX(o.ppo_only) FROM c4_ref_members m JOIN c4_oon_providers o "
         "ON o.file_id = m.file_id AND o.npi = m.npi AND (o.tin = '' OR o.tin = m.tin) WHERE m.file_id = ? "
         "GROUP BY m.ref_id, m.npi")
    for ref, _npi, ppo_only in con.execute(q, (file_id,)):
        hits[ref][0 if ppo_only else 1] += 1
    for ref, in con.execute("SELECT ref_id FROM c4_ref_members WHERE file_id = ?", (file_id,)):
        members[ref] += 1
    n = 0
    for ref, (hp, hs, _) in hits.items():
        if ref not in ev.index:
            continue
        i, m = ev.index[ref], members[ref]
        if hp:
            n += 1
            ev.add(i, NON, w * min(1.0, 0.5 + hp / m), "oon_ppo_plan",
                   f"{hp}/{m} members were paid out of network by a PPO plan (allowed-amount file)")
        if hs:
            ev.flags[i].add("oon_shared")
            ev.notes[i].append(f"note: {hs}/{m} members appear in a shared allowed-amount file (out of network for some plan)")
    return n


def _seed_file_label(ev: Evidence, file_meta: dict, cfg: dict) -> int:
    """C2 placed the file in the PPO list and did not flag it as mixed: every reference is PPO."""
    if not file_meta.get("from_c2") or file_meta.get("needs_network_filter"):
        return 0
    label = str(file_meta.get("c2_network_label") or "")
    if label.upper() in ("MIXED",):
        return 0
    w = cfg["network"]["seed_weights"]["file_label"]
    for i in range(len(ev.ref_ids)):
        ev.add(i, PPO, w, "file_label", f"C2: file is PPO ({label or 'by plan names'})")
    return len(ev.ref_ids)


# ------------------------------------------------------------------ hypothesis 1: dual contracts
def dual_contract(ev: Evidence, con, file_id: int, cfg: dict, log=print) -> dict:
    dc, min_share = cfg["network"]["dual_contract"], cfg["network"]["decide_min_share"]
    out = {"enabled": dc["enabled"], "pairs": 0, "tested": 0, "correct": 0, "precision": None, "used": False}
    if not dc["enabled"]:
        return out
    # Two references of one TIN that list mostly the same NPIs: the same providers under two contracts.
    by_tin: dict[str, dict] = defaultdict(lambda: defaultdict(set))
    for ref, tin, npi in con.execute("SELECT ref_id, tin, npi FROM c4_ref_members WHERE file_id = ? AND tin != '' "
                                     "AND npi IS NOT NULL", (file_id,)):
        by_tin[tin][ref].add(npi)
    pairs = set()
    for refs in by_tin.values():
        if 2 <= len(refs) <= 10:
            rs = sorted(refs)
            for i, a in enumerate(rs):
                for b in rs[i + 1:]:
                    overlap = len(refs[a] & refs[b]) / min(len(refs[a]), len(refs[b]))
                    if overlap >= dc["min_npi_overlap"]:
                        pairs.add((a, b))
    if not pairs:
        return out
    con.execute("DROP TABLE IF EXISTS temp.dc_refs")
    con.execute("CREATE TEMP TABLE dc_refs (ref_id TEXT PRIMARY KEY)")
    con.executemany("INSERT OR IGNORE INTO temp.dc_refs VALUES (?)", [(r,) for p in pairs for r in p])
    rates: dict[str, dict] = defaultdict(dict)
    q = (f"SELECT m.ref_id, p.code, p.billing_class, AVG(p.rate) FROM temp.dc_refs d "
         f"CROSS JOIN c4_refset_members m ON m.file_id = ? AND m.ref_id = d.ref_id "
         f"CROSS JOIN c4_prices p ON p.file_id = m.file_id AND p.refset_id = m.refset_id "
         f"WHERE p.rate > 0 AND p.negotiated_type IN ({','.join('?' * len(DOLLAR_TYPES))}) "
         f"GROUP BY 1, 2, 3")
    for ref, code, bclass, avg in con.execute(q, (file_id, *DOLLAR_TYPES)):
        rates[ref][(code, bclass)] = avg
    calls = []
    for a, b in pairs:
        common = rates[a].keys() & rates[b].keys()
        if len(common) < dc["min_codes_compared"]:
            continue
        ratio = statistics.median(rates[a][k] / rates[b][k] for k in common)
        if ratio >= dc["min_ratio"]:
            calls.append((a, b, ratio))
        elif ratio <= 1 / dc["min_ratio"]:
            calls.append((b, a, 1 / ratio))
    out["pairs"] = len(calls)
    for hi, lo, _ in calls:
        lh, ll = ev.seeded_label(ev.index[hi], min_share), ev.seeded_label(ev.index[lo], min_share)
        if lh and ll and lh != ll:
            out["tested"] += 1
            out["correct"] += lh == PPO
    if out["tested"]:
        out["precision"] = round(out["correct"] / out["tested"], 4)
    out["used"] = out["tested"] >= dc["min_tested"] and (out["precision"] or 0) >= dc["min_precision"]
    log(f"[c4]   hypothesis dual_contract: {len(calls):,} TIN pairs with a price gap; tested on {out['tested']} "
        f"pairs with known networks, precision {out['precision']} -> {'USED' if out['used'] else 'not used'}")
    if out["used"]:
        for hi, lo, ratio in calls:
            ih, il = ev.index[hi], ev.index[lo]
            if ev.seeded_label(ih, min_share) and ev.seeded_label(il, min_share):
                continue
            ev.add(ih, PPO, dc["weight"], "dual_contract", f"paid {ratio:.2f}x the TIN's other reference {lo}")
            ev.add(il, NON, dc["weight"], "dual_contract", f"paid 1/{ratio:.2f} of the TIN's other reference {hi}")
    return out


# ------------------------------------------------------------------ hypothesis 2: label spreading
def _graph(con, file_id: int, ev: Evidence, max_size: int):
    """Reference -> refset links as numpy arrays (integer ids are joined in SQL, read in chunks)."""
    con.execute("DROP TABLE IF EXISTS temp.ref_idx")
    con.execute("CREATE TEMP TABLE ref_idx (ref_id TEXT PRIMARY KEY, i INTEGER)")
    con.executemany("INSERT INTO temp.ref_idx VALUES (?, ?)", ev.index.items())
    q = ("SELECT x.i, s.refset_id, s.size, s.rows FROM c4_refset_stats s "
         "CROSS JOIN c4_refset_members m ON m.file_id = s.file_id AND m.refset_id = s.refset_id "
         "CROSS JOIN temp.ref_idx x ON x.ref_id = m.ref_id "
         "WHERE s.file_id = ? AND s.size BETWEEN 2 AND ?")
    cur = con.execute(q, (file_id, max_size))
    chunks = []
    while True:
        part = cur.fetchmany(1_000_000)
        if not part:
            break
        chunks.append(np.array(part, dtype=np.float64))
    if not chunks:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros(0), 0
    a = np.concatenate(chunks)
    _, si = np.unique(a[:, 1], return_inverse=True)
    return a[:, 0].astype(np.int64), si.astype(np.int64), np.log1p(a[:, 3]) / a[:, 2], int(si.max()) + 1


def spread(F0: np.ndarray, seeded: np.ndarray, ri, si, wt, n_rs: int, iterations: int, alpha: float):
    """Label spreading on the reference <-> refset bipartite graph. Returns (distribution, reach)."""
    n = F0.shape[0]
    F = F0.copy()
    reach = np.zeros(n)
    if len(ri) == 0:
        return F, reach
    for _ in range(iterations):
        prev = F
        G = np.stack([np.bincount(si, weights=F[ri, c], minlength=n_rs) for c in range(2)], axis=1)
        M = np.clip(G[si] - F[ri], 0, None)                # neighbours' labels, without the node itself
        tot = M.sum(axis=1, keepdims=True)
        M = np.divide(M, tot, out=np.zeros_like(M), where=tot > 0) * wt[:, None]
        H = np.stack([np.bincount(ri, weights=M[:, c], minlength=n) for c in range(2)], axis=1)
        reach = H.sum(axis=1)
        Hn = np.divide(H, reach[:, None], out=np.zeros_like(H), where=reach[:, None] > 0)
        F = np.where(seeded[:, None], alpha * F0 + (1 - alpha) * Hn, Hn)
        if np.abs(F - prev).max() < 1e-4:  # converged
            break
    return F, reach


def propagation(ev: Evidence, con, file_id: int, cfg: dict, log=print) -> dict:
    pc, min_share = cfg["network"]["propagation"], cfg["network"]["decide_min_share"]
    out = {"enabled": pc["enabled"], "edges": 0, "holdout_tested": 0, "holdout_accuracy": None, "used": False,
           "reached": 0}
    if not pc["enabled"]:
        return out
    ri, si, wt, n_rs = _graph(con, file_id, ev, pc["max_refset_size"])
    out["edges"] = int(len(ri))
    n = len(ev.ref_ids)
    labels = [ev.seeded_label(i, min_share) for i in range(n)]
    seeded = np.array([lab is not None for lab in labels])
    F0 = np.zeros((n, 2))
    for i, lab in enumerate(labels):
        if lab:
            F0[i, 0 if lab == PPO else 1] = 1.0
    seeds = [i for i in range(n) if seeded[i]]
    per_side = min(sum(1 for lab in labels if lab == PPO), sum(1 for lab in labels if lab == NON))
    out["seeds_ppo"] = sum(1 for lab in labels if lab == PPO)
    out["seeds_non_ppo"] = sum(1 for lab in labels if lab == NON)
    if len(ri) == 0 or per_side < pc["min_seeds_per_side"]:
        # With seeds on one side only, spreading would label every linked reference that side and the
        # held-out check could not fail. Both sides must be present to measure it.
        log(f"[c4]   hypothesis propagation: {out['seeds_ppo']} PPO and {out['seeds_non_ppo']} non-PPO seeds "
            f"(needs {pc['min_seeds_per_side']} of each) -> not used")
        return out
    # k-fold cross-validation: every seed is hidden once and must be recovered from the others
    rnd = random.Random(pc["seed"])
    order = seeds[:]
    rnd.shuffle(order)
    k = max(2, min(pc["folds"], len(order)))
    tested = correct = 0
    for f in range(k):
        hold = set(order[f::k])
        F0h, sh = F0.copy(), seeded.copy()
        for i in hold:
            F0h[i] = 0
            sh[i] = False
        F, reach = spread(F0h, sh, ri, si, wt, n_rs, pc["iterations"], pc["alpha"])
        for i in hold:
            if reach[i] > 0 and F[i].max() >= min_share:
                tested += 1
                correct += (PPO if F[i, 0] >= F[i, 1] else NON) == labels[i]
    out["holdout_tested"] = tested
    out["holdout_accuracy"] = round(correct / tested, 4) if tested else None
    out["used"] = tested >= pc["min_holdout_seeds"] and (out["holdout_accuracy"] or 0) >= pc["min_holdout_accuracy"]
    log(f"[c4]   hypothesis propagation: {len(ri):,} reference-refset links; held-out seeds recovered "
        f"{correct}/{tested} ({out['holdout_accuracy']}) -> {'USED' if out['used'] else 'not used'}")
    if out["used"]:
        F, reach = spread(F0, seeded, ri, si, wt, n_rs, pc["iterations"], pc["alpha"])
        for i in range(n):
            if seeded[i] or reach[i] <= 0:
                continue
            share = F[i].max()
            if share >= min_share:
                out["reached"] += 1
                side = PPO if F[i, 0] >= F[i, 1] else NON
                ev.add(i, side, float(share), "propagation", f"{share:.0%} of co-listed neighbours' evidence")
    return out


# ------------------------------------------------------------------ status
CONFIRMED_PPO, PPO_CANDIDATE, UNRESOLVED, CONFIRMED_NON_PPO = \
    "CONFIRMED_PPO", "PPO_CANDIDATE", "UNRESOLVED", "CONFIRMED_NON_PPO"
STATUSES = [CONFIRMED_PPO, PPO_CANDIDATE, UNRESOLVED, CONFIRMED_NON_PPO]
# Evidence that names this reference's network directly (target-specific). Everything else (TIN-only
# or ambiguous directory matches, shared listings, price-gap and graph hypotheses) is inference.
STRONG = {PPO: {"network_name", "file_label", "directory"},
          NON: {"network_name", "directory", "capitation", "oon_ppo_plan"}}


def network_status(tag: str, ppo_by: dict, non_by: dict, flags: set) -> tuple[str, str]:
    """(status, basis) for one reference, from its decided tag and its evidence per source.

    CONFIRMED_PPO      decided PPO, with direct PPO evidence and no direct non-PPO evidence
    CONFIRMED_NON_PPO  decided non-PPO, with direct non-PPO evidence and no direct PPO evidence
    PPO_CANDIDATE      some PPO evidence (inferred, weak or generic), but no direct proof
    UNRESOLVED         nothing for PPO, or the evidence conflicts
    """
    strong_ppo = sorted(set(ppo_by) & STRONG[PPO])
    strong_non = sorted(set(non_by) & STRONG[NON])
    if strong_ppo and strong_non:
        return UNRESOLVED, f"conflict: PPO by {', '.join(strong_ppo)}; non-PPO by {', '.join(strong_non)}"
    if tag == PPO and strong_ppo:
        return CONFIRMED_PPO, "direct: " + ", ".join(strong_ppo)
    if tag == NON and strong_non:
        return CONFIRMED_NON_PPO, "direct: " + ", ".join(strong_non)
    if strong_non:  # direct non-PPO evidence that did not win the vote: never a PPO candidate
        return UNRESOLVED, "non-PPO by " + ", ".join(strong_non) + ", outweighed by inference"
    if tag == NON:
        return UNRESOLVED, "non-PPO inferred only: " + ", ".join(sorted(non_by))
    if ppo_by:
        return PPO_CANDIDATE, "inferred: " + ", ".join(sorted(ppo_by))
    if "directory_generic" in flags:
        return PPO_CANDIDATE, "listed with the payer, no network named"
    if non_by:
        return UNRESOLVED, "non-PPO inferred only: " + ", ".join(sorted(non_by))
    return UNRESOLVED, "no network evidence"


# ------------------------------------------------------------------ decide
def _confidence(ev: Evidence, i: int, share: float) -> str:
    if ev.sources[i] == {"directory_shared"}:
        return "low"
    direct = ev.sources[i] & {"network_name", "file_label", "directory"}
    if direct and share >= 0.9:
        return "high"
    if share >= 0.8 and ev.sources[i] & {"network_name", "file_label", "directory", "capitation", "dual_contract",
                                          "propagation", "oon_ppo_plan", "directory_weak"}:
        return "medium"
    return "low"


def tag_references(con: sqlite3.Connection, file_id: int, file_meta: dict, cfg: dict, c2cfg: dict,
                   directory=None, log=print) -> dict:
    names = dict(con.execute("SELECT ref_id, network_name FROM c4_refs WHERE file_id = ?", (file_id,)).fetchall())
    ev = Evidence(sorted(names))
    min_share = cfg["network"]["decide_min_share"]
    seeds = {
        "network_name": _seed_network_names(ev, names, cfg, c2cfg),
        "file_label": _seed_file_label(ev, file_meta, cfg),
        "capitation": _seed_capitation(ev, con, file_id, cfg),
        "allowed_amount_ppo_plan": _seed_oon(ev, con, file_id, cfg),
    }
    seeds["directory"], pending = _seed_directory(ev, con, file_id, directory, cfg)
    seeds["directory_by_exclusion"] = _allocate_shared(ev, pending, min_share, final=False)
    log(f"[c4]   network evidence on {len(names):,} references: " + ", ".join(f"{k} {v:,}" for k, v in seeds.items()))
    report = {"references": len(names), "seeded_by": seeds}
    report["dual_contract"] = dual_contract(ev, con, file_id, cfg, log)
    report["seeded_by"]["directory_by_exclusion"] += _allocate_shared(ev, pending, min_share, final=False)
    report["propagation"] = propagation(ev, con, file_id, cfg, log)
    report["seeded_by"]["directory_shared_after_hypotheses"] = _allocate_shared(ev, pending, min_share, final=True)

    rows, counts, statuses = [], defaultdict(int), Counter()
    for i, ref in enumerate(ev.ref_ids):
        p, n = float(ev.ppo[i]), float(ev.non[i])
        if p + n == 0:
            tag, conf, note = UNKNOWN, "none", "; ".join(ev.notes[i])[:500] or "no network evidence"
        else:
            share = max(p, n) / (p + n)
            if share >= min_share:
                tag = PPO if p >= n else NON
                conf = _confidence(ev, i, share)
            else:
                tag, conf = UNKNOWN, "conflict"
            note = "; ".join(ev.notes[i])[:500]
        counts[(tag, conf)] += 1
        status, basis = network_status(tag, ev.ppo_by[i], ev.non_by[i], ev.flags[i])
        statuses[status] += 1
        rows.append((tag, conf, round(p, 4), round(n, 4), note, status, basis, file_id, ref))
    con.executemany("UPDATE c4_refs SET tag = ?, tag_confidence = ?, ppo_evidence = ?, non_ppo_evidence = ?, "
                    "evidence = ?, status = ?, status_basis = ? WHERE file_id = ? AND ref_id = ?", rows)
    con.commit()
    report["tags"] = {f"{t}/{c}": v for (t, c), v in sorted(counts.items())}
    report["statuses"] = {s: statuses.get(s, 0) for s in STATUSES}
    log("[c4]   network tags: " + ", ".join(f"{k} {v:,}" for k, v in report["tags"].items()))
    log("[c4]   network status: " + ", ".join(f"{k} {v:,}" for k, v in report["statuses"].items()))
    return report
