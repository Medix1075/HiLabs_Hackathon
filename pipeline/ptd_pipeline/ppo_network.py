"""Challenge C2 - Identify Centene's California Commercial Group PPO rate files.

Input:  data/index_table.csv (Step 1)
Output: data/c2/ppo_rate_files.csv      definitive PPO rate-file list for C3/C4
        data/c2/decision_log_plans.csv  every evaluated plan row -> status + rule
        data/c2/decision_log_files.csv  every rate file -> vote, inspection, status
        data/c2/decision_log.md         human-readable decision log (Deliverable 2)

Phases
------
1. Market & state  : keep ``group`` market rows whose state resolves to CA.
2. Plan-name rules : classify each plan name as PPO / NON_PPO / UNKNOWN using
                     whole-word keyword rules (so "POST DOC" is not "POS").
3. File vote       : aggregate plan classes per rate file ("guilt by association").
4. Deep inspection : stream the head of each in-scope rate file, read the
                     ``provider_references[].network_name`` labels and confirm /
                     contradict PPO without downloading the whole file.

Usage::

    python -m ptd_pipeline.ppo_network
    python -m ptd_pipeline.ppo_network --min-ppo-share 0.5 --require-network-name
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import ijson
import requests

from .scraper import build_session

log = logging.getLogger("ptd_pipeline.ppo_network")

DEFAULT_INPUT = Path("data") / "index_table.csv"
DEFAULT_OUT_DIR = Path("data") / "c2"
TARGET_STATE = "CA"
TARGET_MARKET = "group"

PPO, NON_PPO, UNKNOWN = "PPO", "NON_PPO", "UNKNOWN"
KEPT, REJECTED = "Kept", "Rejected"


# --------------------------------------------------------------------------- #
# Phase 2 rules - plan-name keywords (whole-word, case-insensitive)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KeywordRule:
    pattern: str
    label: str
    network: str  # PPO or NON_PPO
    prefix_only: bool = False  # abbreviations only trusted in the product slot

    def matches(self, name: str, prefix: str) -> bool:
        return re.search(self.pattern, prefix if self.prefix_only else name, re.IGNORECASE) is not None


PPO_RULES: Tuple[KeywordRule, ...] = (
    KeywordRule(r"\bENHANCED\s*CARE\b", "EnhancedCare PPO", PPO),
    KeywordRule(r"\bPPO\b", "PPO", PPO),
)

# Network-type tokens that are explicitly out of scope.
EXCLUDED_TYPE_RULES: Tuple[KeywordRule, ...] = (
    KeywordRule(r"\bHMO\b", "HMO", NON_PPO),
    KeywordRule(r"\bEPO\b", "EPO", NON_PPO),
    KeywordRule(r"\bPOS\b", "POS", NON_PPO),
    KeywordRule(r"\bHSP\b", "HSP", NON_PPO),
    # Elect Open Access: HMO (PCP + medical group) tier with a PPO self-referral tier.
    KeywordRule(r"\bEOA\b", "EOA (HMO-based Elect Open Access)", NON_PPO),
)

# Known Health Net non-PPO product brands (and their abbreviations in "CA L <slot>" names).
EXCLUDED_PRODUCT_RULES: Tuple[KeywordRule, ...] = (
    KeywordRule(r"\bFULL\s+NETWORK\b", "Full Network", NON_PPO),
    KeywordRule(r"\bWHOLE\s*CARE\b", "WholeCare", NON_PPO),
    KeywordRule(r"\bSMART\s*CARE\b", "SmartCare", NON_PPO),
    KeywordRule(r"\bSC\b", "SmartCare", NON_PPO, prefix_only=True),
    KeywordRule(r"\bCOMMUNITY\s*CARE\b", "CommunityCare", NON_PPO),
    KeywordRule(r"\bSALUD\b", "Salud", NON_PPO),
    KeywordRule(r"\bSAL\b", "Salud", NON_PPO, prefix_only=True),
    KeywordRule(r"\bPURE\s*CARE\b", "PureCare (HSP)", NON_PPO),
)

PRODUCT_PREFIX_TOKENS = 6  # "CA L&T CUS CBA <slot> <type>" fits in the first six tokens


@dataclass
class PlanClass:
    network: str
    status: str
    rule: str
    conflict: bool = False
    signals: List[str] = field(default_factory=list)


def classify_plan_name(name: str) -> PlanClass:
    """Phase 2: classify a plan name. Pure function, no I/O."""
    clean = " ".join((name or "").split())
    if not clean:
        return PlanClass(UNKNOWN, "Abstain", "blank plan name -> abstain (decided at file level)")

    prefix = " ".join(clean.split()[:PRODUCT_PREFIX_TOKENS])
    ppo = _labels(PPO_RULES, clean, prefix)
    types = _labels(EXCLUDED_TYPE_RULES, clean, prefix)
    products = _labels(EXCLUDED_PRODUCT_RULES, clean, prefix)
    excluded = types + [p for p in products if p not in types]
    signals = ppo + excluded

    if ppo and not excluded:
        return PlanClass(PPO, KEPT, f"in ({', '.join(ppo)})", signals=signals)
    if excluded and not ppo:
        return PlanClass(NON_PPO, REJECTED, f"out ({', '.join(excluded)})", signals=signals)
    if ppo and excluded:
        # Conflict resolution: a specific excluded product/option overrides a generic PPO token.
        #   "CA L CUS PPO CA IRONWORKERS FWP EPO$15" -> the group's PPO contract sold as an
        #   EPO option (no out-of-network benefit) -> out of PPO scope.
        reason = "named product overrides PPO token" if products else "excluded option overrides PPO prefix"
        return PlanClass(
            NON_PPO, REJECTED,
            f"out (conflict: {'+'.join(ppo + excluded)} -> {excluded[0]}; {reason})",
            conflict=True, signals=signals,
        )
    return PlanClass(UNKNOWN, "Abstain", "no network keyword -> abstain (decided at file level)")


def _labels(rules: Iterable[KeywordRule], name: str, prefix: str) -> List[str]:
    out: List[str] = []
    for rule in rules:
        if rule.matches(name, prefix) and rule.label not in out:
            out.append(rule.label)
    return out


# --------------------------------------------------------------------------- #
# Phase 1 - market & state
# --------------------------------------------------------------------------- #
STATE_FROM_FILE = re.compile(r"[-_]([a-z]{2})_in-network", re.IGNORECASE)
STATE_FROM_HIOS = re.compile(r"^\d{5}([A-Z]{2})\d{3,}")
CA_NAME_HINTS = re.compile(r"^CA\b|\bCALIFORNIA\b|\bSO\.?\s*CA\b|\bCALCHOICE\b", re.IGNORECASE)


def resolve_state(plan_name: str, plan_id: str, rate_file_url: str) -> Tuple[Optional[str], str]:
    """Return (state, evidence). File token > HIOS plan ID > plan-name hint."""
    m = STATE_FROM_FILE.search(rate_file_url or "")
    if m:
        return m.group(1).upper(), f"rate file token '{m.group(0).strip('-_')}'"
    m = STATE_FROM_HIOS.match(plan_id or "")
    if m:
        return m.group(1).upper(), f"HIOS plan ID {plan_id}"
    if CA_NAME_HINTS.search(plan_name or ""):
        return TARGET_STATE, "plan-name CA indicator"
    return None, "no state indicator"


# --------------------------------------------------------------------------- #
# Phase 4 - stream provider_references.network_name from the head of a rate file
# --------------------------------------------------------------------------- #
class _ByteLimit(Exception):
    pass


class _CountingReader:
    def __init__(self, raw, limit: int):
        self.raw, self.limit, self.count = raw, limit, 0

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        self.count += len(data)
        if self.count > self.limit:
            raise _ByteLimit()
        return data


@dataclass
class Inspection:
    url: str
    verdict: str  # confirmed | contradicted | inconclusive | error
    detail: str
    provider_groups: int = 0
    groups_with_network_name: int = 0
    network_names: Dict[str, int] = field(default_factory=dict)
    bytes_read: int = 0
    file_size: Optional[int] = None
    inspected_at: str = ""


def inspect_network_names(url: str, session: requests.Session, max_bytes: int = 256 << 20) -> Inspection:
    """Stream only the provider_references block; stop at in_network or ``max_bytes``."""
    names: Counter = Counter()
    groups = named = 0
    reader = None
    size = None
    stopped = "end of file"
    try:
        with session.get(url, stream=True, timeout=(30, 300)) as resp:
            resp.raise_for_status()
            size = int(resp.headers["Content-Length"]) if resp.headers.get("Content-Length") else None
            resp.raw.decode_content = True
            reader = _CountingReader(resp.raw, max_bytes)
            seen_refs = False
            group_named = False
            try:
                for prefix, event, value in ijson.parse(reader):
                    if prefix == "" and event == "map_key":
                        if value == "provider_references":
                            seen_refs = True
                        elif seen_refs:  # provider_references finished - nothing more to learn
                            stopped = f"top-level key '{value}'"
                            break
                    elif prefix == "provider_references.item" and event == "start_map":
                        groups += 1
                        group_named = False
                    elif prefix == "provider_references.item.network_name.item" and event == "string":
                        if value.strip():
                            names[value.strip()] += 1
                            group_named = True
                    elif prefix == "provider_references.item" and event == "end_map" and group_named:
                        named += 1
            except _ByteLimit:
                stopped = f"byte limit {max_bytes:,}"
    except (requests.RequestException, ijson.JSONError, ValueError) as exc:
        return Inspection(url, "error", f"{exc.__class__.__name__}: {exc}", bytes_read=reader.count if reader else 0,
                          inspected_at=_now())

    ppo_names = {n: c for n, c in names.items() if re.search(r"\bPPO\b", n, re.IGNORECASE)}
    if groups == 0:
        verdict, detail = "inconclusive", f"no provider_references found before {stopped}"
    elif named == 0:
        verdict, detail = "inconclusive", f"network_name empty in all {groups:,} provider groups"
    elif ppo_names:
        verdict = "confirmed"
        detail = f"PPO network_name present: {', '.join(sorted(ppo_names))} ({named:,}/{groups:,} groups labelled)"
    else:
        verdict = "contradicted"
        detail = f"network_name has no PPO label: {', '.join(n for n, _ in names.most_common(5))}"
    return Inspection(url, verdict, f"{detail}; stopped at {stopped}", groups, named, dict(names.most_common()),
                      reader.count if reader else 0, size, _now())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
@dataclass
class PlanDecision:
    brand: str
    plan_name: str
    plan_id: str
    market_type: str
    rate_file_url: str
    state: str
    phase: str
    plan_class: str
    status: str
    rule: str
    conflict: bool
    final_status: str = ""
    log_line: str = ""


@dataclass
class FileDecision:
    rate_file_url: str
    brand: str
    state: str
    plans_total: int
    plans_in_scope: int
    ppo_votes: int
    non_ppo_votes: int
    abstain: int
    ppo_share: Optional[float]
    vote_result: str
    inspection_verdict: str
    inspection_detail: str
    final_status: str
    rule: str


def load_rows(path: Path) -> Iterable[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def evaluate_plans(rows: Iterable[dict]) -> List[PlanDecision]:
    decisions: List[PlanDecision] = []
    for r in rows:
        name, pid, market, url = r["plan name"], r["plan ID"], r["market type"], r["rate file URL"]
        state, evidence = resolve_state(name, pid, url)
        base = dict(brand=r["brand"], plan_name=name, plan_id=pid, market_type=market,
                    rate_file_url=url, state=state or "")
        if market.strip().lower() != TARGET_MARKET:
            decisions.append(PlanDecision(**base, phase="1-market", plan_class="OUT_OF_SCOPE", status=REJECTED,
                                          rule=f"out ({market or 'blank'} market)", conflict=False))
        elif state != TARGET_STATE:
            decisions.append(PlanDecision(**base, phase="1-state", plan_class="OUT_OF_SCOPE", status=REJECTED,
                                          rule=f"out (state {state or 'unknown'}: {evidence})", conflict=False))
        else:
            pc = classify_plan_name(name)
            decisions.append(PlanDecision(**base, phase="2-name", plan_class=pc.network, status=pc.status,
                                          rule=pc.rule, conflict=pc.conflict))
    return decisions


def vote_files(decisions: Sequence[PlanDecision], min_ppo_share: float) -> Dict[str, dict]:
    by_file: Dict[str, dict] = defaultdict(lambda: {"brand": "", "state": "", "total": 0, PPO: 0, NON_PPO: 0, UNKNOWN: 0})
    for d in decisions:
        f = by_file[d.rate_file_url]
        f["brand"], f["total"] = d.brand, f["total"] + 1
        if d.phase == "2-name":
            f["state"] = d.state
            f[d.plan_class] += 1
        elif not f["state"]:
            f["state"] = d.state
    for f in by_file.values():
        decided = f[PPO] + f[NON_PPO]
        f["in_scope"] = decided + f[UNKNOWN]
        f["share"] = (f[PPO] / decided) if decided else None
        if f["in_scope"] == 0:
            f["vote"] = "out of scope"
        elif decided == 0:
            f["vote"] = "no decidable plans"
        elif f["share"] > min_ppo_share or (min_ppo_share == 0 and f[PPO] > 0):
            f["vote"] = "PPO"
        else:
            f["vote"] = "NON_PPO"
    return by_file


def decide_files(by_file: Dict[str, dict], inspections: Dict[str, Inspection], min_ppo_share: float,
                 require_network_name: bool) -> List[FileDecision]:
    out: List[FileDecision] = []
    for url, f in sorted(by_file.items(), key=lambda kv: (kv[1]["brand"], kv[0])):
        insp = inspections.get(url)
        verdict = insp.verdict if insp else "not inspected"
        detail = insp.detail if insp else ""
        share_txt = f"{f['share']:.0%}" if f["share"] is not None else "n/a"
        votes = f"{f[PPO]} PPO / {f[NON_PPO]} non-PPO / {f[UNKNOWN]} abstain"
        threshold = f"> {min_ppo_share:.0%}" if min_ppo_share else "any PPO plan"

        if f["vote"] == "out of scope":
            status, rule = REJECTED, "out (no CA group plans reference this file)"
        elif f["vote"] != "PPO":
            status, rule = REJECTED, f"out (vote {votes}; PPO share {share_txt} not {threshold})"
        elif verdict == "contradicted":
            status, rule = REJECTED, f"out (vote passed {share_txt}, but network_name contradicts: {detail})"
        elif verdict == "confirmed":
            status, rule = KEPT, f"in (vote {share_txt} PPO; network_name confirmed)"
        elif require_network_name:
            status, rule = REJECTED, f"out (vote passed {share_txt}, network_name {verdict}; --require-network-name)"
        else:
            status, rule = KEPT, f"in (vote {votes}, PPO share {share_txt}; network_name {verdict} - UNVERIFIED)"

        out.append(FileDecision(url, f["brand"], f["state"], f["total"], f["in_scope"], f[PPO], f[NON_PPO],
                                f[UNKNOWN], round(f["share"], 4) if f["share"] is not None else None,
                                f["vote"], verdict, detail, status, rule))
    return out


def finalize_plans(decisions: List[PlanDecision], files: Sequence[FileDecision]) -> None:
    """A plan is Kept only if its own name says PPO *and* its file is Kept.
    Abstaining plans (blank/no keyword) inherit the file's decision (guilt by association)."""
    file_status = {f.rate_file_url: f.final_status for f in files}
    for d in decisions:
        fstat = file_status.get(d.rate_file_url, REJECTED)
        if d.phase != "2-name":
            d.final_status = REJECTED
        elif d.plan_class == PPO:
            d.final_status = fstat
            if fstat == REJECTED:
                d.rule += "; file rejected"
        elif d.plan_class == UNKNOWN:
            d.final_status = fstat
            d.rule += f" -> file {fstat.lower()}"
        else:
            d.final_status = REJECTED
        label = d.plan_name or "<blank plan name>"
        d.log_line = f"{label} -> {'in' if d.final_status == KEPT else 'out'}: {d.rule}"


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def _write_csv(path: Path, rows: Sequence, columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r) if not isinstance(r, dict) else r)


def write_outputs(out_dir: Path, plans: List[PlanDecision], files: List[FileDecision],
                  inspections: Dict[str, Inspection], params: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    kept = [f for f in files if f.final_status == KEPT]
    _write_csv(out_dir / "ppo_rate_files.csv", kept,
               ["rate_file_url", "brand", "state", "ppo_votes", "non_ppo_votes", "abstain", "ppo_share",
                "inspection_verdict", "rule"])
    _write_csv(out_dir / "decision_log_files.csv", files, list(FileDecision.__dataclass_fields__))
    _write_csv(out_dir / "decision_log_plans.csv", plans,
               ["final_status", "log_line", "brand", "plan_name", "plan_id", "market_type", "state", "phase",
                "plan_class", "status", "conflict", "rule", "rate_file_url"])
    (out_dir / "network_inspection.json").write_text(
        json.dumps({u: asdict(i) for u, i in inspections.items()}, indent=2), encoding="utf-8")
    (out_dir / "decision_log.md").write_text(render_markdown(plans, files, params), encoding="utf-8")


def render_markdown(plans: List[PlanDecision], files: List[FileDecision], params: dict) -> str:
    in_scope = [p for p in plans if p.phase == "2-name"]
    lines = [
        "# C2 Decision Log - Centene CA Commercial Group PPO",
        "",
        f"Generated {_now()} from `{params['input']}`.  ",
        f"Policy: PPO share threshold `{params['threshold']}`, "
        f"require network_name confirmation: `{params['require_network_name']}`.",
        "",
        "## Funnel",
        "",
        "| Stage | Rows |",
        "|---|---|",
        f"| Index rows evaluated | {len(plans):,} |",
        f"| Rejected - not group market | {sum(p.phase == '1-market' for p in plans):,} |",
        f"| Rejected - group but not CA | {sum(p.phase == '1-state' for p in plans):,} |",
        f"| CA group rows classified by name | {len(in_scope):,} |",
        f"| -> PPO by name | {sum(p.plan_class == PPO for p in in_scope):,} |",
        f"| -> non-PPO by name | {sum(p.plan_class == NON_PPO for p in in_scope):,} |",
        f"| -> abstain (blank / no keyword) | {sum(p.plan_class == UNKNOWN for p in in_scope):,} |",
        f"| -> conflicts resolved | {sum(p.conflict for p in in_scope):,} |",
        f"| **Final kept plan rows** | **{sum(p.final_status == KEPT for p in plans):,}** |",
        "",
        "## Rate-file decisions",
        "",
        "| Status | Brand | State | PPO / non-PPO / abstain | PPO share | network_name | Rule | File |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for f in files:
        if f.plans_in_scope == 0:
            continue
        share = f"{f.ppo_share:.0%}" if f.ppo_share is not None else "n/a"
        lines.append(f"| **{f.final_status}** | {f.brand} | {f.state} | {f.ppo_votes}/{f.non_ppo_votes}/{f.abstain} "
                     f"| {share} | {f.inspection_verdict} | {f.rule} | `{f.rate_file_url.rsplit('/', 1)[-1]}` |")
    out_scope = [f for f in files if f.plans_in_scope == 0]
    lines += ["", f"{len(out_scope)} other rate files had no CA group plans and were rejected in Phase 1 "
                  "(see `decision_log_files.csv`).", "", "## Phase 2 rule frequency (CA group rows)", "",
              "| Rows | Rule |", "|---|---|"]
    for rule, n in Counter(p.rule.split(";")[0] for p in in_scope).most_common():
        lines.append(f"| {n:,} | {rule} |")
    conflicts = sorted({p.log_line for p in in_scope if p.conflict})
    if conflicts:
        lines += ["", "## Conflicts", ""] + [f"- {c}" for c in conflicts]
    lines += ["", "## Sample decisions", ""]
    seen_rules: Dict[str, int] = Counter()
    for p in in_scope:
        key = p.rule.split(";")[0]
        if seen_rules[key] < 3:
            lines.append(f"- {p.log_line}")
            seen_rules[key] += 1
    lines += ["", "Full per-row log: `decision_log_plans.csv`.", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration / CLI
# --------------------------------------------------------------------------- #
def run(input_path: Path = DEFAULT_INPUT, out_dir: Path = DEFAULT_OUT_DIR, min_ppo_share: float = 0.5,
        require_network_name: bool = False, inspect: bool = True, refresh_inspection: bool = False,
        max_inspect_bytes: int = 256 << 20) -> List[FileDecision]:
    plans = evaluate_plans(load_rows(input_path))
    by_file = vote_files(plans, min_ppo_share)

    cache_path = out_dir / "network_inspection.json"
    cache: Dict[str, Inspection] = {}
    if cache_path.exists() and not refresh_inspection:
        cache = {u: Inspection(**v) for u, v in json.loads(cache_path.read_text(encoding="utf-8")).items()}

    inspections: Dict[str, Inspection] = {}
    if inspect:
        session = build_session()
        # Inspect every file with CA group plans (not only vote winners) so the log documents evidence for both.
        for url, f in by_file.items():
            if f["in_scope"] == 0:
                continue
            if url in cache and cache[url].verdict != "error":
                inspections[url] = cache[url]
                log.info("Phase 4 (cached) %s -> %s", url.rsplit("/", 1)[-1], cache[url].verdict)
                continue
            log.info("Phase 4 streaming network_name from %s", url.rsplit("/", 1)[-1])
            inspections[url] = inspect_network_names(url, session, max_inspect_bytes)
            i = inspections[url]
            log.info("  %s: %s (read %s bytes)", i.verdict, i.detail, f"{i.bytes_read:,}")

    files = decide_files(by_file, inspections, min_ppo_share, require_network_name)
    finalize_plans(plans, files)
    params = {"input": str(input_path), "threshold": f"> {min_ppo_share:.0%}" if min_ppo_share else "any PPO plan",
              "require_network_name": require_network_name}
    write_outputs(out_dir, plans, files, inspections, params)

    for f in files:
        if f.plans_in_scope:
            log.info("%-8s %s | %s", f.final_status, f.rate_file_url.rsplit("/", 1)[-1], f.rule)
    kept = sum(f.final_status == KEPT for f in files)
    log.info("PPO rate files kept: %d -> %s", kept, out_dir / "ppo_rate_files.csv")
    log.info("Decision log: %s", out_dir / "decision_log.md")
    if kept == 0:
        log.warning("No rate file passed the PPO rules - see decision_log.md for why.")
    return files


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="C2: identify CA commercial group PPO rate files.")
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Step 1 table (default: %(default)s)")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="Output folder (default: %(default)s)")
    p.add_argument("--min-ppo-share", type=float, default=0.5,
                   help="File is PPO if PPO votes / decided votes exceeds this (0 = any PPO plan). Default: %(default)s")
    p.add_argument("--require-network-name", action="store_true",
                   help="Reject files whose network_name cannot confirm PPO (default: keep, flagged UNVERIFIED)")
    p.add_argument("--skip-inspection", action="store_true", help="Skip Phase 4 network_name streaming")
    p.add_argument("--refresh-inspection", action="store_true", help="Ignore cached Phase 4 results")
    p.add_argument("--max-inspect-mb", type=int, default=256, help="Max MB to stream per file in Phase 4")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not a.input.exists():
        log.error("%s not found - run `python -m ptd_pipeline` (Steps 0-1) first.", a.input)
        return 1
    run(a.input, a.out_dir, a.min_ppo_share, a.require_network_name, not a.skip_inspection,
        a.refresh_inspection, a.max_inspect_mb << 20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
