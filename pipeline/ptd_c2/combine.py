"""Step 6: combine signals in priority order and score confidence.

Priority: network_name (Step 4) > file association (Step 3) > plan name (Step 2) > supporting (Step 5).
Every row gets a one-line reason; every disagreement goes to the conflicts log.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .supporting import HMO_LEAN, PPO_LEAN

LEVELS = ["low", "medium", "high"]
PPO_NET = {"PPO_FULL": "FULL", "PPO_ENHANCEDCARE": "ENHANCEDCARE", "PPO_MIXED_VARIANTS": "FULL+ENHANCEDCARE"}
NON_PPO_NET = {"HMO", "HSP", "EPO", "POS"}


def _shift(level: str, step: int, cap: str = "high") -> str:
    i = max(0, min(LEVELS.index(level) + step, LEVELS.index(cap)))
    return LEVELS[i]


def _agrees(final: str, signal: str) -> bool | None:
    if signal == PPO_LEAN:
        return final in ("PPO", "POS")
    if signal == HMO_LEAN:
        return final in ("HMO", "EPO", "HSP")
    return None


def _name_variant(row: dict) -> str:
    return "ENHANCEDCARE" if row.get("name_ec") else "FULL"


def classify_row(row: dict, vote: dict, probe: dict | None, cfg: dict, conflicts: list,
                 supporting_enabled: dict | None = None) -> None:
    def conflict(kind, detail, resolution):
        conflicts.append({"row_id": row["row_id"], "plan_name": row["plan_name"], "plan_id": row["plan_id"],
                          "rate_file_url": row["rate_file_url"], "conflict_type": kind, "detail": detail,
                          "resolution": resolution})

    non_ppo_types = set(cfg["file_vote"]["non_ppo_types"])
    name = row["name_signal"]
    name_explicit = row["name_strength"] in ("explicit", "product_code")
    fv = vote["file_vote"]
    vote_txt = f"file vote {fv} ({vote['ppo_votes']} PPO / {vote['non_ppo_votes']} non-PPO of {vote['plan_rows']} rows)"
    net = probe.get("network_label") if probe else None
    final, variant, source, conf, needs_filter = "UNKNOWN", "", "", "low", False
    primary_disagreement = False
    parts = []
    supporting_enabled = supporting_enabled if supporting_enabled is not None else {"oon_signal": True, "payment_signal": True}

    if name == "conflict":
        conflict("name_conflict", f"plan name carries several network types: {row['name_types']}",
                 "resolved by network_name / file vote, not keyword order")

    if net in PPO_NET or net in NON_PPO_NET or net == "NON_PPO_MIXED":
        source = "network_name"
        names_txt = ", ".join(f"'{n}'" for n in list(probe["network_names"])[:4])
        if net in PPO_NET:
            final, variant = "PPO", PPO_NET[net]
        elif net in NON_PPO_NET:
            final = net
        else:
            final = name if name in non_ppo_types else "HMO"
        conf = "high"
        parts.append(f"rate file network_name {names_txt} -> {net}")
        disagree_name = name_explicit and name not in ("conflict", "none") and \
            (name == "PPO") != (final == "PPO")
        if disagree_name:
            conf, primary_disagreement = "medium", True
            conflict("name_vs_network_name", f"name says {name}, network_name says {net}", f"network_name wins -> {final}")
        if fv in ("PPO", "NON_PPO") and (fv == "PPO") != (final == "PPO"):
            conflict("vote_vs_network_name", f"{vote_txt}, network_name says {net}", f"network_name wins -> {final}")
        if probe.get("sampled_partially"):
            parts.append(f"(first {probe['provider_references_read']} provider refs sampled)")
    elif net == "MIXED":
        needs_filter = True
        source = "network_name (mixed file) + plan name/vote"
        ppo_names = ", ".join(f"'{n}'" for n in probe.get("ppo_network_names", []))
        parts.append(f"rate file mixes PPO ({ppo_names}) and non-PPO networks")
        if name == "PPO" or (name in ("none", "conflict") and fv == "PPO"):
            final, variant = "PPO", _name_variant(row)
            conf = "medium" if name_explicit else "low"
        elif name in non_ppo_types or fv == "NON_PPO":
            final = name if name in non_ppo_types else vote["dominant_non_ppo_type"] or "HMO"
            conf = "medium" if name_explicit else "low"
        conflict("mixed_file", f"file carries PPO and non-PPO network names: {probe.get('network_labels')}",
                 f"plan placed by name/vote -> {final}; C4 must filter provider_references to PPO network names")
    else:
        if net:
            parts.append(f"network_name not confirmed ({probe.get('status')}: {net})")
        decided = vote["ppo_votes"] + vote["non_ppo_votes"]
        own_vote = 1 if name not in ("none", "conflict") else 0
        if decided - own_vote < 1:
            fv = "NO_OTHER_VOTES"  # the only vote is this plan's own name: not association evidence
        if fv == "PPO":
            final, variant, source = "PPO", _name_variant(row), "file vote"
        elif fv == "NON_PPO":
            final = name if name in non_ppo_types else vote["dominant_non_ppo_type"]
            source = "file vote"
        elif name not in ("none", "conflict"):
            final, source = name, "plan name"
            if final == "PPO":
                variant = _name_variant(row)
        if source == "file vote":
            conf = "medium"
            disagree = name not in ("none", "conflict") and (name == "PPO") != (final == "PPO")
            if disagree:
                conf, primary_disagreement = "low", True
                conflict("name_vs_vote", f"name says {name}, {vote_txt}", f"file vote wins -> {final}")
            elif name in ("none", "conflict"):
                parts.append("no usable name signal; inherited file label")
        elif source == "plan name":
            # An explicit keyword or product code is the best plan-level evidence left when the
            # file is shared by PPO and non-PPO plans; hints and benefit-string shapes stay low.
            conf = "medium" if name_explicit else "low"
            if fv == "NO_OTHER_VOTES":
                parts.append("no other plan shares this rate file")
            if fv == "MIXED":
                needs_filter = True
                conflict("mixed_vote", vote_txt, f"plan name decides -> {final}; file shared with non-PPO plans")
        parts.append(vote_txt)

    if name != "none":
        parts.append(f"name says {name} [{row['name_tokens']}]")

    # Optional employer tiebreaker (very weak)
    if final == "UNKNOWN" and cfg["combine"]["employer_tiebreaker"] and row.get("_employer_types"):
        types = row["_employer_types"]
        if len(types) == 1:
            final = next(iter(types))
            variant = "FULL" if final == "PPO" else ""
            source, conf = "employer tiebreaker", "low"
            parts.append(f"same employer's other plans are all {final}")

    # Supporting evidence: adjusts confidence only
    agree = disagree = 0
    for label, field in (("OON", "oon_signal"), ("payment", "payment_signal")):
        sig, measured = row[field], row[field.replace("_signal", "_measured")]
        if not supporting_enabled.get(field):
            continue  # hypothesis not supported in this run: logged, not used
        a = _agrees(final, sig)
        if a is True:
            agree += 1
        elif a is False:
            disagree += 1
            conflict("supporting_vs_final", f"{label}: {sig} ({measured}) vs final {final}",
                     "supporting evidence never overrides; confidence lowered")
    if final != "UNKNOWN":
        # High needs network_name; a primary-signal disagreement caps at medium;
        # a plan-name-only decision stays low (names are a weak tiebreaker).
        cap = "high" if source == "network_name" and not primary_disagreement else "medium"
        if source == "employer tiebreaker" or (source == "plan name" and not name_explicit):
            cap = "low"
        if agree and not disagree:
            conf = _shift(conf, +1, cap)
            parts.append(f"supporting agrees ({agree})")
        elif disagree and not agree:
            conf = _shift(conf, -1)
            parts.append(f"supporting disagrees ({disagree})")
    else:
        parts.append("no decisive signal")
        conflict("unresolved", f"name {name}, {vote_txt}, network {net or 'not probed'}", "left UNKNOWN, not in scope")

    include_ec = cfg["network_probe"]["include_enhancedcare_ppo"]
    in_scope_ppo = final == "PPO" and (include_ec or variant != "ENHANCEDCARE")
    if final == "PPO" and not in_scope_ppo:
        parts.append("EnhancedCare PPO excluded by config")
    row.update(final_network_type=final, ppo_variant=variant, decided_by=source, confidence=conf,
               in_scope_ppo=in_scope_ppo, needs_network_filter=needs_filter,
               reason=f"{final}: " + "; ".join(parts) + f" -> {conf}")


def gate_supporting(rows: list[dict], probes: dict, cfg: dict) -> tuple[dict, list[dict]]:
    """Test the Step 5 hypotheses against rows whose file has a single-type network_name.

    A supporting signal only moves confidence if it agrees with network_name often enough in THIS run.
    """
    gate = cfg["name_signals"]["hypothesis_gate"]
    truth = []
    for r in rows:
        net = (probes.get(r["rate_file_url"]) or {}).get("network_label")
        if net in PPO_NET:
            truth.append((r, "PPO"))
        elif net in NON_PPO_NET:
            truth.append((r, net))
    enabled, report = {}, []
    for field, desc in (("oon_signal", "OON file with records => PPO/POS; empty/missing => HMO/EPO"),
                        ("payment_signal", "mostly ffs + many TINs => PPO; capitation => HMO")):
        cross = Counter((t, r[field]) for r, t in truth)
        decisive = [(t, r[field]) for r, t in truth if r[field] in (PPO_LEAN, HMO_LEAN)]
        n = len(decisive)
        agree = sum(1 for t, sig in decisive if _agrees(t, sig))
        precision = agree / n if n else None
        ok = n >= gate["min_support"] and precision >= gate["min_precision"]
        enabled[field] = ok
        report.append({
            "hypothesis": f"{field}: {desc}", "implies": "PPO or HMO lean",
            "support_on_labelled_plans": n, "agree": agree,
            "precision": round(precision, 4) if precision is not None else "",
            "breakdown": {f"{k[0]}|{k[1]}": v for k, v in sorted(cross.items())},
            "unlabelled_plans_it_would_tag": sum(1 for r in rows if r[field] in (PPO_LEAN, HMO_LEAN)) - n,
            "used_in_classification": ok,
            "verdict": ("SUPPORTED by network_name: adjusts confidence" if ok else
                        "NOT ENOUGH network_name-confirmed rows: logged only" if n < gate["min_support"] else
                        "REJECTED (disagrees with network_name): logged only"),
        })
    return enabled, report


def combine(rows_all: list[dict], votes: dict, probes: dict, cfg: dict) -> tuple[list[dict], list[dict]]:
    """Classify every row (out-of-scope rows included, so nothing is silently dropped).

    Returns (conflicts, supporting-hypothesis report).
    """
    conflicts: list[dict] = []
    placeholders = set(cfg["combine"]["placeholder_plan_ids"])
    in_scope = [r for r in rows_all if r["in_scope"]]
    enabled, report = gate_supporting(in_scope, probes, cfg)
    by_employer = defaultdict(list)
    for r in in_scope:
        if r["plan_id"] and r["plan_id"] not in placeholders:
            by_employer[r["plan_id"]].append(r)
    for r in in_scope:
        others = {o["name_signal"] for o in by_employer.get(r["plan_id"], []) if o is not r}
        r["_employer_types"] = {t for t in others if t not in ("none", "conflict")}
        classify_row(r, votes[r["rate_file_url"]], probes.get(r["rate_file_url"]), cfg, conflicts, enabled)
    for r in rows_all:
        if not r["in_scope"]:
            r.update(final_network_type="OUT_OF_SCOPE", ppo_variant="", decided_by="scope filter", confidence="high",
                     in_scope_ppo=False, needs_network_filter=False, reason=f"OUT_OF_SCOPE: {r['scope_reason']}")
    return conflicts, report


def ppo_file_list(rows_all: list[dict], probes: dict) -> list[dict]:
    """Distinct in-scope PPO rate files: the hand-off to C3 (quality) and C4 (rates)."""
    by_url = defaultdict(list)
    for r in rows_all:
        if r.get("in_scope_ppo"):
            by_url[r["rate_file_url"]].append(r)
    out = []
    for url, rs in sorted(by_url.items()):
        p = probes.get(url) or {}
        conf = Counter(r["confidence"] for r in rs)
        out.append({
            "rate_file_url": url,
            "ppo_variants": ",".join(sorted({r["ppo_variant"] for r in rs})),
            "network_label": p.get("network_label", "not probed"),
            "ppo_network_names": "|".join(p.get("ppo_network_names", [])),
            "needs_network_filter": any(r["needs_network_filter"] for r in rs),
            "ppo_plan_rows": len(rs),
            "decided_by": ",".join(sorted({r["decided_by"] for r in rs})),
            "best_confidence": max((r["confidence"] for r in rs), key=LEVELS.index),
            "confidence_counts": dict(conf),
            "allowed_amount_file_urls": "|".join(sorted({u for r in rs for u in r.get("_oon_urls") or () if u})),
        })
    return out


def allowed_amount_files(rows_all: list[dict], oon_results: dict) -> list[dict]:
    """Every allowed-amount (out-of-network) file listed by an in-scope plan, with who lists it.

    ppo_only is the key fact for C4: providers in a file that only PPO plans list were paid out of
    network by a PPO plan, so they are not in that PPO network. A file shared with HMO plans only
    says "out of network for some plan".
    """
    by_url: dict[str, dict] = {}
    for r in rows_all:
        if not r.get("in_scope"):
            continue
        for u in r.get("_oon_urls") or ():
            if not u:
                continue
            d = by_url.setdefault(u, {"ppo": 0, "other": 0, "files": set()})
            d["ppo" if r.get("in_scope_ppo") else "other"] += 1
            d["files"].add(r["rate_file_url"])
    out = []
    for u, d in sorted(by_url.items()):
        res = oon_results.get(u) or {}
        out.append({"allowed_amount_file_url": u, "ppo_plan_rows": d["ppo"], "non_ppo_plan_rows": d["other"],
                    "ppo_only": d["ppo"] > 0 and d["other"] == 0, "rate_files": "|".join(sorted(d["files"])),
                    "status": res.get("status", "not probed"), "records_seen": res.get("records_seen", ""),
                    "bytes_read": res.get("bytes_read", ""), "error": res.get("error", "")})
    return out
