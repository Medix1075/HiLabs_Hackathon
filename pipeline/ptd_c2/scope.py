"""Step 1: hard scope filters (market, brand, state) applied before any network-type logic.

Every removed row gets an exclusion-log entry naming each rule it failed, primary rule first.
The word "PPO" plays no part here: Ambetter "Gold PPO" (individual) and QualChoice "PPO Complete"
(Arkansas) must fall to the market and state rules.
"""
from __future__ import annotations

from .normalize import norm_brand, state_from_file_url, state_from_name, state_from_plan_id


def resolve_state(row: dict, cfg: dict) -> tuple[str | None, str]:
    """Returns (state, source). Sources in order of trust: plan name prefix, HIOS id, file name, brand."""
    for source, value in (
        ("plan_name_prefix", state_from_name(row["plan_name_norm"])),
        ("hios_plan_id", state_from_plan_id(row["plan_id"])),
        ("rate_file_name", state_from_file_url(row["rate_file_url"])),
    ):
        if value:
            return value, source
    default = cfg["scope"]["brand_default_state"].get(norm_brand(row["brand"]))
    if default:
        return default, "brand_default (inferred)"
    return None, "unknown"


def apply_scope(rows: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Annotates every row with scope fields; returns (in_scope_rows, exclusion_log)."""
    sc = cfg["scope"]
    keep_markets = {m.lower() for m in sc["keep_market_types"]}
    keep_brands = {norm_brand(b) for b in sc["keep_brands"]}
    target = sc["target_state"].upper()
    kept, excluded = [], []
    for row in rows:
        brand = norm_brand(row["brand"])
        state, state_src = resolve_state(row, cfg)
        row["brand_norm"] = brand
        row["state"] = state or ""
        row["state_source"] = state_src
        failures = []
        market = (row["market_type"] or "").strip().lower()
        if market not in keep_markets:
            failures.append(("market", f"market type is '{row['market_type'] or 'blank'}', not {'/'.join(sorted(keep_markets))}"))
        if brand not in keep_brands:
            reason = next((r for b, r in sc["brand_exclusion_reasons"].items() if b in brand),
                          sc["default_brand_exclusion_reason"])
            failures.append(("brand", reason))
        if state is None:
            failures.append(("state", "no state found in plan name, plan id, file name or brand default"))
        elif state != target:
            failures.append(("state", f"state is {state} (from {state_src}), not {target}"))
        elif sc.get("require_explicit_state") and state_src.startswith("brand_default"):
            failures.append(("state", f"state {state} only inferred from the brand and require_explicit_state is on"))

        row["in_scope"] = not failures
        row["scope_reason"] = "; ".join(f"[{r}] {msg}" for r, msg in failures) if failures else (
            f"group market, Health Net, state {state} ({state_src})")
        if failures:
            excluded.append({
                "row_id": row["row_id"], "brand": row["brand"], "plan_name": row["plan_name"],
                "plan_id": row["plan_id"], "market_type": row["market_type"], "rate_file_url": row["rate_file_url"],
                "primary_rule": failures[0][0], "all_rules_failed": ",".join(r for r, _ in failures),
                "reason": row["scope_reason"],
                "name_mentions_ppo": "PPO" in row["plan_name_norm"].split(),
            })
        else:
            kept.append(row)
    return kept, excluded
