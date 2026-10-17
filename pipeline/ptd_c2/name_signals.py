"""Step 2: plan-name signals (weak; used as a tiebreaker and as votes for Step 3).

Explicit keywords and product codes are trusted. The hypothesis signals (EnhancedCare, L&T/Life
hints, benefit-string shape) are measured first and only used if they clear the config gate.
"""
from __future__ import annotations

from collections import Counter

def tag_name(norm_name: str, cfg: dict) -> dict:
    """Return the raw matches for one normalised plan name (no hypothesis gating yet)."""
    ns = cfg["name_signals"]
    keyword_hits = {t: True for t, rxs in ns["_keywords"].items() if any(rx.search(norm_name) for rx in rxs)}
    code_hits: dict[str, list[str]] = {}
    for t, codes in ns["_product_codes"].items():
        for code, rx in codes.items():
            if rx.search(norm_name):
                code_hits.setdefault(t, []).append(code)
    ec = any(rx.search(norm_name) for rx in ns["_enhancedcare"])
    hints = [name for name, (_, rxs) in ns["_hints"].items() if any(rx.search(norm_name) for rx in rxs)]
    shapes = [name for name, (_, rx) in ns["_shapes"].items() if rx.search(norm_name)]
    return {"keywords": keyword_hits, "codes": code_hits, "ec": ec, "hints": hints, "shapes": shapes}


def _strong_types(tags: dict) -> tuple[set[str], list[str], bool]:
    """Types implied by explicit keywords + product codes, plus EC when nothing else is present."""
    types = set(tags["keywords"]) | set(tags["codes"])
    tokens = [f"kw:{t}" for t in tags["keywords"]] + [f"code:{c}" for cs in tags["codes"].values() for c in cs]
    ec_used = False
    if tags["ec"]:
        if types:
            tokens.append(f"EC(ignored: next to {'/'.join(sorted(types))})")
        else:
            types.add("PPO")
            tokens.append("code:EC/EnhancedCare")
            ec_used = True
    return types, tokens, ec_used


def evaluate_hypotheses(rows: list[dict], cfg: dict) -> tuple[dict, list[dict]]:
    """Measure each hint/shape against plans whose type is known from explicit keywords/codes.

    Returns (enabled_map name->bool, hypothesis report rows).
    """
    ns = cfg["name_signals"]
    gate = ns["hypothesis_gate"]
    report, enabled = [], {}
    labelled = []
    for r in rows:
        types, _, _ = _strong_types(r["_tags"])
        if len(types) == 1:
            labelled.append((r, next(iter(types))))

    def score(name, implied, matcher):
        hits = [t for r, t in labelled if matcher(r)]
        support = len(hits)
        correct = sum(1 for t in hits if t == implied)
        precision = correct / support if support else None
        coverage_unlabelled = sum(1 for r in rows if matcher(r) and len(_strong_types(r["_tags"])[0]) == 0)
        ok = support >= gate["min_support"] and precision is not None and precision >= gate["min_precision"]
        enabled[name] = ok
        report.append({
            "hypothesis": name, "implies": implied, "support_on_labelled_plans": support,
            "agree": correct, "precision": round(precision, 4) if precision is not None else "",
            "breakdown": dict(Counter(hits)), "unlabelled_plans_it_would_tag": coverage_unlabelled,
            "used_in_classification": ok,
            "verdict": ("SUPPORTED: used as a weak signal" if ok else
                        "NOT ENOUGH EVIDENCE: logged only" if support < gate["min_support"] else
                        "REJECTED: precision below gate, logged only"),
        })

    for name, (implied, _) in ns["_hints"].items():
        score(f"hint:{name}", implied, lambda r, n=name: n in r["_tags"]["hints"])
    for name, (implied, _) in ns["_shapes"].items():
        score(f"shape:{name}", implied, lambda r, n=name: n in r["_tags"]["shapes"])

    # EC hypothesis: how often does EC co-occur with HMO vs PPO vs nothing?
    ec_rows = [r for r in rows if r["_tags"]["ec"]]
    ec_with = Counter()
    for r in ec_rows:
        t = set(r["_tags"]["keywords"]) | set(r["_tags"]["codes"])
        ec_with["with HMO" if "HMO" in t else "with PPO" if "PPO" in t else "no other type" if not t else "other"] += 1
    report.append({
        "hypothesis": "code:EC = EnhancedCare PPO unless next to HMO", "implies": "PPO",
        "support_on_labelled_plans": len(ec_rows), "agree": ec_with.get("with PPO", 0),
        "precision": "", "breakdown": dict(ec_with), "unlabelled_plans_it_would_tag": ec_with.get("no other type", 0),
        "used_in_classification": True,
        "verdict": "Applied as specified; EC next to HMO is ignored. Confirmed or overturned by network_name in Step 4.",
    })
    return enabled, report


def assign_name_signals(rows: list[dict], cfg: dict) -> list[dict]:
    """Annotate in-scope rows with name_signal, matched tokens and strength. Returns the hypothesis report."""
    ns = cfg["name_signals"]
    for r in rows:
        r["_tags"] = tag_name(r["plan_name_norm"], cfg)
    enabled, report = evaluate_hypotheses(rows, cfg)
    for r in rows:
        tags = r["_tags"]
        types, tokens, ec_used = _strong_types(tags)
        strength = "explicit" if tags["keywords"] else "product_code" if types else ""
        if not types:
            weak = set()
            for name in tags["hints"]:
                if enabled.get(f"hint:{name}"):
                    weak.add(ns["_hints"][name][0])
                    tokens.append(f"hint:{name}")
            for name in tags["shapes"]:
                if enabled.get(f"shape:{name}"):
                    weak.add(ns["_shapes"][name][0])
                    tokens.append(f"shape:{name}")
            if weak:
                types, strength = weak, "hint"
        # record disabled hypothesis matches too, for transparency
        tokens += [f"(unused hint:{h})" for h in tags["hints"] if not enabled.get(f"hint:{h}") and f"hint:{h}" not in tokens]
        tokens += [f"(unused shape:{s})" for s in tags["shapes"] if not enabled.get(f"shape:{s}") and f"shape:{s}" not in tokens]
        if len(types) > 1:
            signal = "conflict"
        elif types:
            signal = next(iter(types))
        else:
            signal = "none"
        r["name_signal"] = signal
        r["name_types"] = ",".join(sorted(types))
        r["name_tokens"] = "; ".join(tokens)
        r["name_strength"] = strength if signal != "none" else ""
        r["name_ec"] = ec_used
    return report
