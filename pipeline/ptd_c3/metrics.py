"""Turn a FileScan's counts into the C3 report card: nine metrics, each with a raw value, a 0-1 score
and a one-line justification, then a weighted 0-100 score and a letter grade per file and bucket."""
from __future__ import annotations

from datetime import date

from .scan import Acc, FileScan
from .tags import ALL


def _share(n: int, d: int) -> float | None:
    return n / d if d else None


def _tol_score(share: float | None, tol: float) -> float | None:
    return None if share is None else max(0.0, 1.0 - min(1.0, share / tol))


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.2f}%"


def _metric(name, value, score, why, measured=True) -> dict:
    return {"metric": name, "value": value, "score": None if score is None else round(score, 4),
            "measured": measured and score is not None, "justification": why}


def file_age_days(last_updated: str | None, as_of: date) -> int | None:
    try:
        return (as_of - date.fromisoformat(str(last_updated)[:10])).days
    except (TypeError, ValueError):
        return None


def bucket_metrics(scan: FileScan, bucket: str, cfg: dict, expected: dict[str, set] | None) -> list[dict]:
    a: Acc = scan.acc[bucket]
    tol, rc = cfg["tolerance"], cfg["rates"]
    out = []

    # 1. schema validity (file level: the same for every bucket)
    err = sum(scan.schema_errors.values())
    share = _share(err, scan.schema_checked)
    top_missing = [k for k in cfg["schema"]["top_required"] if k not in scan.top and k != "in_network"]
    if "in_network" not in scan.order:
        top_missing.append("in_network")
    s = _tol_score(share, tol["schema_error_share"])
    if s is not None and top_missing:
        s *= 0.5
    worst = ", ".join(f"{k} {v:,}" for k, v in scan.schema_errors.most_common(4)) or "none"
    out.append(_metric("schema_validity", _pct(share), s,
                       f"{err:,} problems in {scan.schema_checked:,} records checked against the CMS v2.0.0 required "
                       f"fields, enums and code formats (top: {worst})"
                       + (f"; top-level fields missing {top_missing}, score halved" if top_missing else "")
                       + f". Scores 0 at {_pct(tol['schema_error_share'])}."))

    # 2. code coverage
    if expected:
        want = expected["(all)"]
        hit = len(want & a.codes)
        cov = _share(hit, len(want))
        out.append(_metric("code_coverage", _pct(cov), cov,
                           f"{hit:,} of {len(want):,} expected billing codes appear in the file "
                           f"({len(a.codes):,} distinct codes in total)."))
    else:
        out.append(_metric("code_coverage", f"{len(a.codes):,} distinct codes", None,
                           "Not measured: no expected-code list (the HiLabs specialty to billing code Excel has not "
                           "been provided). Pass --expected-codes to measure it. Weight spread over the other metrics.",
                           measured=False))

    # 3. provider coverage
    res = _share(a.resolved, a.rows)
    out.append(_metric("provider_coverage", _pct(res), res,
                       f"{a.resolved:,} of {a.rows:,} price rows link to a provider group with a valid NPI; "
                       f"{len(a.npis):,} distinct valid NPIs and {len(a.tins):,} TINs across {a.groups:,} provider groups. "
                       "California-only counts need the NPPES extract (C5)."))

    # 4. $0 rates
    z = _share(a.zero, a.rows)
    out.append(_metric("zero_rates", _pct(z), _tol_score(z, tol["zero_rate_share"]),
                       f"{a.zero:,} price rows have a rate of 0 or less. Scores 0 at {_pct(tol['zero_rate_share'])}."))

    # 5. outliers
    o = _share(a.outliers, a.outlier_tested)
    if not a.outlier_tested:
        out.append(_metric("outliers", "n/a", None,
                           f"Not measured: no group of {cfg['outliers']['min_group_size']}+ comparable rates to test.",
                           measured=False))
    else:
        out.append(_metric("outliers", _pct(o), _tol_score(o, tol["outlier_share"]),
                       f"{a.outliers:,} of {a.outlier_tested:,} tested rates fall outside log-IQR fences "
                       f"(k={cfg['outliers']['iqr_k']}, at least {cfg['outliers']['min_ratio_to_median']}x from the median) "
                       "of rates for the same code, type, class, setting and modifiers, or are percentages above "
                       f"{rc['max_percentage']}. Scores 0 at {_pct(tol['outlier_share'])}."))

    # 6. rate-type mix
    dollar = sum(n for t, n in a.neg_types.items() if t in rc["_dollar"])
    per_diem = sum(n for t, n in a.neg_types.items() if t in rc["_per_diem"])
    pct = sum(n for t, n in a.neg_types.items() if t in rc["_percentage"])
    d = _share(dollar, a.rows)
    mix = ", ".join(f"{t} {_pct(_share(n, a.rows))}" for t, n in a.neg_types.most_common())
    out.append(_metric("rate_type_mix", _pct(d), d,
                       f"Share of price rows that are dollar amounts comparable across providers ({mix or 'no rows'}). "
                       f"Per diem ({per_diem:,}) and percentage ({pct:,}) rows can't be compared with them directly."))

    # 7. duplicates
    du = _share(a.dups, a.rows)
    out.append(_metric("duplicates", _pct(du), _tol_score(du, tol["duplicate_share"]),
                       f"{a.dups:,} price rows repeat an earlier row exactly (same code, providers, type, rate, class, "
                       f"setting, service codes, modifiers and expiry; checked {scan.dedup_scope}-wide). "
                       f"Scores 0 at {_pct(tol['duplicate_share'])}."))

    # 8. staleness
    ex = _share(a.expired, a.rows)
    ex_s = _tol_score(ex, tol["expired_share"])
    age = file_age_days(scan.top.get("last_updated_on"), cfg["_as_of"])
    st = cfg["staleness"]
    age_s = None if age is None else max(0.0, min(1.0, (st["stale_days"] - age) / (st["stale_days"] - st["fresh_days"])))
    parts = [x for x in (ex_s, age_s) if x is not None]
    out.append(_metric("staleness", f"expired {_pct(ex)}, file age {age if age is not None else 'unknown'} days",
                       sum(parts) / len(parts) if parts else None,
                       f"{a.expired:,} rows expired before {cfg['_as_of']} ({a.evergreen:,} evergreen 9999-12-31 rows "
                       f"count as current); last_updated_on {scan.top.get('last_updated_on', 'missing')}. "
                       f"Average of the expired score (0 at {_pct(tol['expired_share'])}) and the age score "
                       f"(1 up to {st['fresh_days']} days, 0 at {st['stale_days']})."))

    # 9. junk signals
    junk_groups = a.groups_tin_only + a.groups_placeholder_tin + a.groups_invalid_npi
    jg = _share(junk_groups, a.groups)
    ca = _share(a.catch_all, a.rows)
    sub = [x for x in (_tol_score(jg, tol["junk_group_share"]), _tol_score(ca, tol["catch_all_share"])) if x is not None]
    out.append(_metric("junk", f"groups {_pct(jg)}, catch-all rows {_pct(ca)}", sum(sub) / len(sub) if sub else None,
                       f"Provider groups: {a.groups_tin_only:,} TIN-only (no NPI or npi [0]), {a.groups_placeholder_tin:,} "
                       f"placeholder TINs, {a.groups_invalid_npi:,} with an NPI failing the check digit. "
                       f"{a.catch_all:,} rows on catch-all codes (CSTM-ALL and the like). "
                       f"Average of the group score (0 at {_pct(tol['junk_group_share'])}) and catch-all score "
                       f"(0 at {_pct(tol['catch_all_share'])})."))
    return out


def overall(metrics: list[dict], cfg: dict) -> dict:
    w = cfg["weights"]
    used = [m for m in metrics if m["measured"]]
    total_w = sum(w[m["metric"]] for m in used)
    score = 100 * sum(w[m["metric"]] * m["score"] for m in used) / total_w if total_w else None
    grade = "n/a"
    if score is not None:
        grade = next((g for g, cut in sorted(cfg["grades"].items(), key=lambda kv: -kv[1]) if score >= cut), "F")
    skipped = [m["metric"] for m in metrics if not m["measured"]]
    return {"score": None if score is None else round(score, 1), "grade": grade,
            "metrics_used": len(used), "metrics_not_measured": ",".join(skipped)}


def grade_scan(scan: FileScan, cfg: dict, expected: dict[str, set] | None) -> tuple[list[dict], list[dict]]:
    """Returns (score rows, metric rows), one score row per bucket."""
    score_rows, metric_rows = [], []
    buckets = [ALL] + sorted(b for b in scan.acc if b != ALL)
    for b in buckets:
        ms = bucket_metrics(scan, b, cfg, expected)
        ov = overall(ms, cfg)
        a = scan.acc[b]
        row = {"rate_file_url": scan.url, "bucket": b, **ov, "status": scan.status, "error": scan.error,
               "sampled": scan.status == "sampled", "bytes_read": scan.bytes_read, "seconds": scan.seconds,
               "in_network_items": a.items, "price_rows": a.rows, "distinct_codes": len(a.codes),
               "provider_groups": a.groups, "distinct_npis": len(a.npis), "distinct_tins": len(a.tins),
               "last_updated_on": scan.top.get("last_updated_on", ""), "version": scan.top.get("version", ""),
               "reporting_entity_name": scan.top.get("reporting_entity_name", ""),
               "blank_network_names": scan.blank_network_names}
        for m in ms:
            row[f"{m['metric']}_score"] = m["score"]
        score_rows.append(row)
        for m in ms:
            metric_rows.append({"rate_file_url": scan.url, "bucket": b, **m, "weight": cfg["weights"][m["metric"]]})
    return score_rows, metric_rows


def code_coverage_rows(scan: FileScan, expected: dict[str, set]) -> list[dict]:
    rows = []
    for b in [ALL] + sorted(x for x in scan.acc if x != ALL):
        codes = scan.acc[b].codes
        for spec, want in sorted(expected.items()):
            hit = want & codes
            rows.append({"rate_file_url": scan.url, "bucket": b, "specialty": spec, "expected_codes": len(want),
                         "present": len(hit), "coverage": round(len(hit) / len(want), 4) if want else None,
                         "missing_sample": ",".join(sorted(want - codes)[:20])})
    return rows
