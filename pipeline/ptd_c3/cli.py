"""Command line entry point.

  python -m ptd_c3 run                          # grade the PPO rate files C2 listed in out/c2/
  python -m ptd_c3 run --run-c2 --run-c1        # C1, then C2, then C3
  python -m ptd_c3 run --files a.json.gz https://.../b.json --out out/c3
  python -m ptd_c3 run --expected-codes specialty_codes.xlsx --provider-tags tags.csv
  python -m ptd_c3 run --max-bytes 200000000    # grade a sample of each file (flagged as sampled)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from ptd_c2.cli import main as run_c2
from ptd_c2.cli import write_csv, write_xlsx

from . import __version__
from .codes import load_expected_codes
from .config import load_config
from .metrics import code_coverage_rows, grade_scan
from .scan import scan_file
from .tags import load_tag_table

DEFAULT_C2_FILES = Path("out/c2/c2_ppo_rate_files.csv")
C2_CARRY = ["ppo_variants", "network_label", "ppo_network_names", "needs_network_filter", "best_confidence"]
SCORE_FIELDS = [
    "rate_file_url", "bucket", "score", "grade", "metrics_used", "metrics_not_measured",
    "schema_validity_score", "code_coverage_score", "provider_coverage_score", "zero_rates_score",
    "outliers_score", "rate_type_mix_score", "duplicates_score", "staleness_score", "junk_score",
    "status", "sampled", "error", "bytes_read", "seconds", "in_network_items", "price_rows", "distinct_codes",
    "provider_groups", "distinct_npis", "distinct_tins", "last_updated_on", "version", "reporting_entity_name",
    "blank_network_names", *C2_CARRY,
]
METRIC_FIELDS = ["rate_file_url", "bucket", "metric", "value", "score", "weight", "measured", "justification"]


def _load_c2_files(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh) if r.get("rate_file_url")]


def run(args) -> int:
    if args.run_c2:
        c2_args = ["run"] + (["--run-c1"] if args.run_c1 else []) + (["--no-probe"] if args.c2_no_probe else [])
        print("[c2] running C2 first", flush=True)
        if run_c2(c2_args) != 0:
            print("[c2] C2 failed; C3 not run", flush=True)
            return 1
    cfg = load_config(args.config, args.as_of)
    if args.max_bytes is not None:
        cfg["stream"]["max_bytes"] = args.max_bytes
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    started = datetime.now(timezone.utc)
    log(f"C3 v{__version__} started {started.isoformat()} config={cfg['_path']} as_of={cfg['_as_of']}")

    if args.files:
        targets = [{"rate_file_url": f} for f in args.files]
        source = "--files"
    else:
        c2_path = Path(args.c2 or DEFAULT_C2_FILES)
        if not c2_path.exists():
            print(f"C2 file list {c2_path} not found. Run C2 first (python -m ptd_c2 run), add --run-c2, "
                  "or pass --files.", file=sys.stderr)
            return 1
        targets = _load_c2_files(c2_path)
        source = str(c2_path)
    log(f"[input] {len(targets)} rate file(s) from {source}")
    for t in targets:
        if str(t.get("needs_network_filter", "")).lower() == "true":
            log(f"[input] {t['rate_file_url'].rsplit('/', 1)[-1]}: C2 says it mixes PPO and non-PPO providers "
                "with no usable network_name, so it is graded as a whole (bucket ALL). "
                "Pass --provider-tags to also grade the PPO providers alone.")

    expected = None
    if args.expected_codes:
        expected = load_expected_codes(args.expected_codes, args.expected_codes_sheet)
        log(f"[input] expected codes: {len(expected.codes):,} codes, {len(expected.by_specialty)} specialties "
            f"from {expected.source}; " + ("weighted by specialty and code volume" if expected.weights else "unweighted"))
        if expected.unweighted_specialties:
            log(f"[input] no specialty weight, left out of the weighted coverage: "
                f"{'; '.join(expected.unweighted_specialties)}")
    else:
        log("[input] no expected-code list: code coverage not measured, its weight spread over the other metrics")
    tagger = None
    if args.provider_tags:
        tagger = load_tag_table(args.provider_tags)
        log(f"[input] provider tags: {len(tagger.by_npi):,} NPIs, {len(tagger.by_tin):,} TINs")
    if cfg["stream"]["max_bytes"]:
        log(f"[input] sampling: at most {cfg['stream']['max_bytes']:,} decompressed bytes per file")

    scans = {}
    with ThreadPoolExecutor(max_workers=max(1, cfg["stream"]["workers"])) as pool:
        futs = {pool.submit(scan_file, t["rate_file_url"], cfg, tagger, log): t for t in targets}
        for fut in as_completed(futs):
            sc = fut.result()
            scans[sc.url] = sc
            log(f"[c3] {sc.status:<8} {sc.acc['ALL'].rows:>12,} rows {sc.bytes_read:>15,} bytes "
                f"{sc.seconds:>8}s {sc.url}" + (f" ({sc.error})" if sc.error else ""))

    score_rows, metric_rows, example_rows, schema_rows, coverage_rows = [], [], [], [], []
    for t in targets:
        sc = scans[t["rate_file_url"]]
        s_rows, m_rows = grade_scan(sc, cfg, expected)
        for r in s_rows:
            r.update({k: t.get(k, "") for k in C2_CARRY})
        score_rows += s_rows
        metric_rows += m_rows
        for issue, exs in sorted(sc.examples.items()):
            for e in exs:
                example_rows.append({"rate_file_url": sc.url, "issue": issue, "example": json.dumps(e)})
        for kind, n in sc.schema_errors.most_common():
            schema_rows.append({"rate_file_url": sc.url, "problem": kind, "records": n})
        if expected:
            coverage_rows += code_coverage_rows(sc, expected)
    for r in score_rows:
        if r["bucket"] == "ALL":
            log(f"[score] {r['score']} ({r['grade']}) {r['rate_file_url'].rsplit('/', 1)[-1]}"
                + (" [sampled]" if r["sampled"] else "") + (f" [{r['status']}]" if r["status"] == "error" else ""))

    files = {
        "c3_file_scores.csv": (score_rows, SCORE_FIELDS),
        "c3_metrics.csv": (metric_rows, METRIC_FIELDS),
        "c3_schema_problems.csv": (schema_rows, ["rate_file_url", "problem", "records"]),
        "c3_examples.csv": (example_rows, ["rate_file_url", "issue", "example"]),
    }
    if expected:
        files["c3_code_coverage.csv"] = (coverage_rows, None)
    for name, (data, fields) in files.items():
        write_csv(out / name, data, fields)
    write_xlsx(out / "c3_results.xlsx", {n.removeprefix("c3_").removesuffix(".csv"): v for n, v in files.items()})
    summary = {
        "version": __version__, "started": started.isoformat(), "finished": datetime.now(timezone.utc).isoformat(),
        "config": cfg["_path"], "as_of": str(cfg["_as_of"]), "source": source, "files": len(targets),
        "sampled_max_bytes": cfg["stream"]["max_bytes"], "expected_codes": bool(expected),
        "provider_tags": bool(tagger), "weights": cfg["weights"],
        "scores": {r["rate_file_url"]: {"score": r["score"], "grade": r["grade"], "status": r["status"]}
                   for r in score_rows if r["bucket"] == "ALL"},
    }
    (out / "c3_run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "c3_run_log.txt").write_text("\n".join(log_lines) + "\n")
    log(f"[done] outputs in {out}")
    return 0 if all(s.status != "error" for s in scans.values()) else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ptd_c3", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="grade each PPO rate file's data quality")
    r.add_argument("--c2", help=f"C2 rate-file list (default {DEFAULT_C2_FILES})")
    r.add_argument("--files", nargs="+", help="grade these rate files (paths or URLs) instead of C2's list")
    r.add_argument("--run-c2", action="store_true", help="run C2 first (python -m ptd_c2 run)")
    r.add_argument("--run-c1", action="store_true", help="with --run-c2: run C1 before C2")
    r.add_argument("--c2-no-probe", action="store_true", help="with --run-c2: run C2 without reading rate files")
    r.add_argument("--expected-codes", help="CSV/XLSX of expected billing codes, e.g. the HiLabs Rate Benchmarking Individual Weights workbook")
    r.add_argument("--expected-codes-sheet", help="sheet name for --expected-codes")
    r.add_argument("--provider-tags", help="CSV/XLSX with npi and/or tin and a tag column: also grade each tag")
    r.add_argument("--config", help="config YAML (default config/c3.yaml)")
    r.add_argument("--as-of", help="grade against this date (YYYY-MM-DD); default today")
    r.add_argument("--max-bytes", type=int, help="read at most this many decompressed bytes per file (a sample)")
    r.add_argument("--out", default="out/c3", help="output directory")
    r.set_defaults(func=run)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
