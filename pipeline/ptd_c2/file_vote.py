"""Step 3: "guilt by association". Plans sharing a rate file vote on what the file is.

plan_id is never used as a key: Health Net reuses employer EINs and 999999999 across PPO and HMO plans.
"""
from __future__ import annotations

from collections import Counter, defaultdict


def vote_files(rows: list[dict], cfg: dict) -> dict[str, dict]:
    fv = cfg["file_vote"]
    non_ppo = set(fv["non_ppo_types"])
    by_file: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_file[r["rate_file_url"]].append(r)

    votes = {}
    for url, plans in by_file.items():
        c = Counter(p["name_signal"] for p in plans)
        ppo = c.get("PPO", 0)
        nonppo = sum(c.get(t, 0) for t in non_ppo)
        decided = ppo + nonppo
        ppo_share = ppo / decided if decided else None
        if decided < fv["min_decided_votes"]:
            label = "NO_VOTES"
        elif ppo_share >= fv["label_threshold"]:
            label = "PPO"
        elif (1 - ppo_share) >= fv["label_threshold"]:
            label = "NON_PPO"
        else:
            label = "MIXED"
        top_non_ppo = max(non_ppo, key=lambda t: c.get(t, 0)) if nonppo else ""
        votes[url] = {
            "rate_file_url": url, "plan_rows": len(plans), "ppo_votes": ppo, "non_ppo_votes": nonppo,
            "conflict_votes": c.get("conflict", 0), "no_signal": c.get("none", 0),
            "vote_breakdown": dict(c), "ppo_share": round(ppo_share, 4) if ppo_share is not None else "",
            "non_ppo_share": round(1 - ppo_share, 4) if ppo_share is not None else "",
            "threshold": fv["label_threshold"], "file_vote": label, "dominant_non_ppo_type": top_non_ppo,
        }
    for r in rows:
        r["file_vote"] = votes[r["rate_file_url"]]["file_vote"]
    return votes
