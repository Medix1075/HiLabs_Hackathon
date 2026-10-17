"""Turn the stored price rows into one clean rate per provider x billing code x price variant.

Steps (rules and their order are in config/c4.yaml, `extract`):

1. Pairs and signatures. Every (NPI, TIN) pair is linked to the references it belongs to, keeping
   only references whose network status is included (all but CONFIRMED_NON_PPO by default) and,
   of those, only the ones with the pair's best status. The sorted set of those references is the
   pair's signature. Pairs with the same signature have identical prices, so each signature is
   worked out once and fanned out to its pairs only when rows are written.
2. Split first. Rates for one code are split by negotiated_type, billing_class, setting, place of
   service (service_code) and modifiers. These are different prices for different situations,
   not duplicates, so each combination (a "variant") keeps its own rate.
3. Clean. Drop capitation rows (a per-member amount, not a service price), rates of 0 or less,
   missing rates and expired rates (9999-12-31 counts as evergreen). Exact repeats merge.
4. Pick. When a variant still has several distinct rates, the tie-break picks one and records
   which rule decided it: network status, then latest expiry, then support, then the lower rate.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict

from .db import stable_id
from .network import CONFIRMED_PPO, PPO_CANDIDATE, UNRESOLVED

CONF_RANK = {"high": 3, "medium": 2, "low": 1}
STATUS_RANK = {CONFIRMED_PPO: 3, PPO_CANDIDATE: 2, UNRESOLVED: 1}


def build_pairs(con: sqlite3.Connection, file_id: int, include_statuses: list[str], log=print) -> dict:
    q = (f"SELECT m.npi, m.tin, m.tin_type, m.ref_id, r.status FROM c4_ref_members m "
         f"JOIN c4_refs r ON r.file_id = m.file_id AND r.ref_id = m.ref_id "
         f"WHERE m.file_id = ? AND m.npi IS NOT NULL AND r.status IN ({','.join('?' * len(include_statuses))}) "
         f"ORDER BY m.npi, m.tin")
    pairs, sig_rows, seen = [], [], set()
    cur_key, refs, tin_type = None, {}, ""

    def close():
        if cur_key is None:
            return
        # A pair in several contracts takes its rates only from the contracts with the best status:
        # once one reference is confirmed PPO, the pair's other references are other networks.
        best = max(STATUS_RANK.get(st, 0) for st in refs.values())
        rs = sorted(r for r, st in refs.items() if STATUS_RANK.get(st, 0) == best)
        sig = stable_id(file_id, *rs)
        pairs.append((file_id, cur_key[0], cur_key[1], tin_type, sig))
        if sig not in seen:
            seen.add(sig)
            sig_rows.extend((file_id, sig, r) for r in rs)

    for npi, tin, ttype, ref, st in con.execute(q, (file_id, *include_statuses)):
        if (npi, tin) != cur_key:
            close()
            cur_key, refs, tin_type = (npi, tin), {}, ttype
        refs[ref] = st
    close()
    con.executemany("INSERT OR IGNORE INTO c4_pairs VALUES (?,?,?,?,?)", pairs)
    con.executemany("INSERT INTO c4_sig_members VALUES (?,?,?)", sig_rows)
    con.execute("CREATE INDEX IF NOT EXISTS ix_sig_members ON c4_sig_members (file_id, sig_id)")
    con.commit()
    excluded = con.execute(
        f"SELECT COUNT(DISTINCT m.npi || '|' || m.tin) FROM c4_ref_members m JOIN c4_refs r "
        f"ON r.file_id = m.file_id AND r.ref_id = m.ref_id WHERE m.file_id = ? AND m.npi IS NOT NULL "
        f"AND r.status NOT IN ({','.join('?' * len(include_statuses))})", (file_id, *include_statuses)).fetchone()[0]
    tin_only = con.execute("SELECT COUNT(DISTINCT ref_id) FROM c4_ref_members WHERE file_id = ? AND npi IS NULL",
                           (file_id,)).fetchone()[0]
    info = {"pairs": len(pairs), "signatures": len(seen), "pairs_in_excluded_refs_only_or_partly": excluded,
            "tin_only_references": tin_only}
    log(f"[c4]   {len(pairs):,} NPI-TIN pairs in {len(seen):,} distinct reference signatures "
        f"({tin_only:,} references have TIN-only groups and no NPI, so no NPI rows)")
    return info


def _pick(cands: list[tuple], order: list[str]):
    """cands: (rate, expiry, net_rank, tag, conf, support, status). Returns the winner and the deciding rule."""
    keys = {
        "network": lambda c: -c[2],
        "expiry": lambda c: tuple(-ord(ch) for ch in c[1]),
        "support": lambda c: -c[5],
        "lowest": lambda c: c[0],
    }
    ranked = sorted(cands, key=lambda c: tuple(keys[k](c) for k in order))
    if len(ranked) == 1:
        return ranked[0], "single"
    a, b = ranked[0], ranked[1]
    for k in order:
        if keys[k](a) != keys[k](b):
            return a, k
    return a, order[-1]


def pick_rates(con: sqlite3.Connection, file_id: int, cfg: dict, as_of: str, log=print) -> dict:
    ec = cfg["extract"]
    excl = {a.lower() for a in ec["exclude_arrangements"]}
    evergreen = set(ec["evergreen_dates"])
    order = ec["tie_break"]
    q = ("SELECT sm.sig_id, p.rowid, p.code_type, p.code, p.negotiated_type, p.billing_class, p.setting, p.pos, "
         "p.modifier, p.rate, p.expiration_date, p.arrangement, r.tag, r.tag_confidence, r.status "
         "FROM c4_sig_members sm "  # CROSS JOIN fixes the join order: signature -> refsets -> prices
         "CROSS JOIN c4_refset_members rm ON rm.file_id = sm.file_id AND rm.ref_id = sm.ref_id "
         "CROSS JOIN c4_prices p ON p.file_id = rm.file_id AND p.refset_id = rm.refset_id "
         "JOIN c4_refs r ON r.file_id = sm.file_id AND r.ref_id = sm.ref_id "
         "WHERE sm.file_id = ? "
         "ORDER BY sm.sig_id, p.code_type, p.code, p.negotiated_type, p.billing_class, p.setting, p.pos, p.modifier")
    dropped, rules = Counter(), Counter()
    out: list[tuple] = []
    cur, rows_seen = None, set()
    by_rate: dict[float, list] = {}
    n_cands = 0

    def close():
        if cur is None or not by_rate:
            return
        cands = [(rate, v[0], v[1], v[2], v[3], v[4], v[5]) for rate, v in by_rate.items()]
        win, rule = _pick(cands, order)
        rules[rule] += 1
        rates = [c[0] for c in cands]
        out.append((file_id, cur[0], *cur[1:], win[0], win[1], win[3], win[4], n_cands, len(cands),
                    min(rates), max(rates), rule, win[6]))
        if len(out) >= 50000:
            flush()

    def flush():
        con.executemany("INSERT INTO c4_sig_rates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
        con.commit()
        out.clear()

    for (sig, rowid, ctype, code, ntype, bclass, setting, pos, mod, rate, exp, arr, tag, conf, status) in \
            con.execute(q, (file_id,)):
        key = (sig, ctype, code, ntype, bclass, setting, pos, mod)
        if key != cur:
            close()
            cur, rows_seen, by_rate, n_cands = key, set(), {}, 0
        if rowid in rows_seen:  # same price row reached through two references of this signature
            continue
        rows_seen.add(rowid)
        if arr in excl:
            dropped[f"arrangement_{arr}"] += 1
            continue
        if rate is None:
            dropped["rate_missing"] += 1
            continue
        if ec["drop_zero"] and rate <= 0:
            dropped["rate_zero_or_negative"] += 1
            continue
        if ec["drop_expired"] and exp and exp not in evergreen and exp < as_of:
            dropped["expired"] += 1
            continue
        n_cands += 1
        net = STATUS_RANK.get(status, 0) * 10 + CONF_RANK.get(conf, 0)
        exp_key = exp or "0000-00-00"
        v = by_rate.get(rate)
        if v is None:
            by_rate[rate] = [exp_key, net, tag, conf, 1, status]
        else:
            if net > v[1]:
                v[1], v[2], v[3], v[5] = net, tag, conf, status
            v[0] = max(v[0], exp_key)
            v[4] += 1
            dropped["exact_repeat_merged"] += 1
    close()
    flush()
    con.execute("CREATE INDEX IF NOT EXISTS ix_sig_rates ON c4_sig_rates (file_id, sig_id)")
    con.commit()
    variants = sum(rules.values())
    log(f"[c4]   {variants:,} signature x code x variant rates; tie-break rule used: "
        + ", ".join(f"{k} {v:,}" for k, v in rules.most_common()))
    log("[c4]   dropped price rows: " + (", ".join(f"{k} {v:,}" for k, v in dropped.most_common()) or "none"))
    return {"signature_rates": variants, "pick_rules": dict(rules), "dropped": dict(dropped)}


def fanout_counts(con: sqlite3.Connection, file_id: int) -> dict:
    row = con.execute(
        "SELECT COUNT(*), COUNT(DISTINCT p.npi), COUNT(DISTINCT p.tin), COUNT(DISTINCT s.code) "
        "FROM c4_pairs p JOIN c4_sig_rates s ON s.file_id = p.file_id AND s.sig_id = p.sig_id WHERE p.file_id = ?",
        (file_id,)).fetchone()
    return {"rate_rows": row[0], "npis": row[1], "tins": row[2], "codes": row[3]}


def tag_table_rows(con: sqlite3.Connection, file_id: int) -> list[dict]:
    """(npi, tin, tag) rows in the layout C3's --provider-tags reads (tag = network status)."""
    out = defaultdict(set)
    for npi, tin, tag in con.execute("SELECT m.npi, m.tin, r.status FROM c4_ref_members m JOIN c4_refs r "
                                     "ON r.file_id = m.file_id AND r.ref_id = m.ref_id WHERE m.file_id = ?", (file_id,)):
        out[(npi or "", tin)].add(tag)
    return [{"npi": k[0], "tin": k[1], "tag": t} for k, tags in out.items() for t in sorted(tags)]
