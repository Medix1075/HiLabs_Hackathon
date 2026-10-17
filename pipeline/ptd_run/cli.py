"""Run the whole pipeline in order, then write the summary file (deliverable 4).

  python -m ptd_run all --workbook "Rate Benchmarking Individual Weights_Hackathon.xlsx" \\
      --nppes data/public/NPPES_Data_Dissemination_October_2026.zip --dac data/public/DAC_NationalDownloadableFile.csv
  python -m ptd_run all --from c4 --workbook ...          # reuse C1-C3 outputs, start at C4
  python -m ptd_run all --fetch-public --workbook ...     # download NPPES, CMS D&C and NUCC first
  python -m ptd_run summary                               # only rebuild the summary from existing outputs

Every stage can also be run on its own (python -m ptd_c4 run, ...); this runner only chains them
with the same arguments and stops at the first stage that fails.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .docs import write_logic, write_quality_metrics

STAGES = ["c1", "c2", "c3", "c4", "c3net", "c5", "c6", "c7"]


def _load(path: str | Path) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def _count_csv(path: str | Path) -> int | None:
    p = Path(path)
    if not p.exists():
        return None
    with open(p, newline="", encoding="utf-8-sig") as fh:
        return max(0, sum(1 for _ in csv.reader(fh)) - 1)


def _input_warnings(c4: dict, c5: dict) -> list[str]:
    """Inputs whose absence changes the numbers above, so nobody mistakes a missing file for a finding."""
    w = []
    if c4 and not c4.get("directory"):
        w.append("No payer directory (C4 --directory): no provider can be confirmed PPO, so deliverable 1 holds only "
                 "rates confirmed some other way (usually none) and the rest are in the candidate file (1b). Build "
                 "the directory table with `python -m ptd_dir` (see README, 'PPO directory').")
    if c4 and c4.get("code_scope") == "all":
        w.append("No HiLabs workbook (--workbook): every billing code was extracted and C7 weights codes equally.")
    used = (c5.get("sources_used") or {}) if c5 else None
    if used is not None:
        if "nppes" not in used and "npi_api" not in used:
            w.append("No NPPES file (C5 --nppes): no taxonomy, name or NPPES address for any NPI, so most addresses "
                     "and specialties are missing. Pass the monthly NPPES ZIP with --nppes, or let C5 use the NPI "
                     "Registry API (--npi-api auto, the default, needs internet access).")
        if "dac" not in used:
            w.append("No CMS Doctors and Clinicians file (C5 --dac): one fewer independent address source.")
        if "zip_county" not in used:
            w.append("No ZIP-county file (C5 --zip-county): C7's regions come from ZIP3 areas, not counties.")
    return w


def write_summary(out: Path, data: Path) -> dict:
    """Deliverable 4: one page of run statistics, from whatever stage outputs exist."""
    c2, c3, c4 = _load(out / "c2/c2_run_summary.json"), _load(out / "c3/c3_run_summary.json"), _load(out / "c4/c4_run_summary.json")
    c5, c6, c7 = _load(out / "c5/c5_run_summary.json"), _load(out / "c6/c6_run_summary.json"), _load(out / "c7/c7_run_summary.json")
    c7c = _load(out / "c7_candidates/c7_run_summary.json")
    c6f = (c6.get("files") or {}) if c6 else {}
    toc = _load(data / "toc_urls.json")
    s = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "index_files_found": len(toc.get("links", toc.get("toc_urls", []))) if toc else None,
        "index_files_parsed": len(glob.glob(str(data / "raw" / "*"))) or None,
        "plan_file_rows": _count_csv(data / "index_table.csv"),
        "unique_rate_files": _count_csv(data / "rate_files.csv"),
        "in_scope_plan_rows": c2.get("in_scope_rows"), "ppo_plan_rows": c2.get("in_scope_ppo_rows"),
        "ppo_rate_files": c2.get("ppo_rate_files"), "plan_types": c2.get("final_types"),
        "allowed_amount_files": (f"{c2.get('allowed_amount_files')} listed by in-scope plans, "
                                 f"{c2.get('allowed_amount_files_ppo_only')} listed only by PPO plans")
        if c2.get("allowed_amount_files") is not None else None,
        "file_quality_scores": c3.get("scores"),
        "rate_files_processed": len([f for f in c4.get("files", []) if f.get("status") == "ok"]) if c4 else None,
        "price_rows_read": sum(f.get("price_rows") or 0 for f in c4.get("files", [])) if c4 else None,
        "file_shape": {f["url"]: {"MB": round((f.get("size_bytes") or 0) / 1e6, 1), "references": f.get("references"),
                                  "in_network_items": f.get("items"), "price_rows_all_codes": f.get("all_price_rows"),
                                  "distinct_codes": f.get("distinct_codes"),
                                  "workbook_codes_found": f.get("workbook_codes_found"),
                                  "items_by_code_type": f.get("code_types")}
                       for f in c4.get("files", []) if f.get("status") == "ok"} if c4 else None,
        "network_status": c4.get("network_status") if c4 else None,
        "network_tags": {u: r.get("tags") for u, r in c4.get("network", {}).items()} if c4 else None,
        "rates_extracted": (c4.get("totals") or {}).get("rate_rows"),
        "unique_npis": c4.get("distinct_npis"), "unique_tins": c4.get("distinct_tins"),
        "pct_npis_with_address": c5.get("pct_npis_with_address"),
        "pct_npis_with_verified_address": c5.get("pct_npis_with_verified_address"),
        "pct_tins_with_address": c5.get("pct_tins_with_address"),
        "pct_tins_with_verified_address": c5.get("pct_tins_with_verified_address"),
        "best_address_vs_nppes": c5.get("best_address_vs_nppes_practice"),
        "address_sources_used": (", ".join(sorted(c5.get("sources_used") or {})) or "none") if c5 else None,
        "rate_file_rows": (c6f.get("c6_provider_rate_file") or {}).get("rows"),
        "rate_file_rows_with_address": (c6f.get("c6_provider_rate_file") or {}).get("rows_with_address"),
        "candidate_file_rows": (c6f.get("c6_provider_rate_file_candidates") or {}).get("rows"),
        "specialty_sources": c6.get("specialty_sources"),
        "providers_scored": c7.get("providers_scored"), "tins_scored": c7.get("tins_scored"),
        "cost_tiers": c7.get("tiers"),
        "candidate_providers_scored": c7c.get("providers_scored"),
    }
    labels = [
        ("Index files found on the Centene page", "index_files_found"), ("Index files parsed", "index_files_parsed"),
        ("Plan x rate-file rows (C1)", "plan_file_rows"), ("Unique rate files (C1)", "unique_rate_files"),
        ("CA commercial group plan rows (C2)", "in_scope_plan_rows"), ("PPO plan rows (C2)", "ppo_plan_rows"),
        ("PPO rate files (C2)", "ppo_rate_files"), ("Plan types (C2)", "plan_types"),
        ("Allowed-amount (out-of-network) files (C2)", "allowed_amount_files"),
        ("File quality score per file (C3)", "file_quality_scores"), ("Rate files processed (C4)", "rate_files_processed"),
        ("Rate file shape (C4)", "file_shape"), ("Price rows read for in-scope codes (C4)", "price_rows_read"), ("Network status of provider references (C4)", "network_status"),
        ("Network evidence vote / confidence (C4)", "network_tags"),
        ("Rates extracted: NPI x TIN x code x variant (C4)", "rates_extracted"), ("Unique NPIs (C4)", "unique_npis"),
        ("Unique TINs (C4)", "unique_tins"), ("% NPIs linked to an address (C5)", "pct_npis_with_address"),
        ("% NPIs linked to a verified address (C5)", "pct_npis_with_verified_address"),
        ("% TINs linked to an address (C5)", "pct_tins_with_address"),
        ("% TINs linked to a verified address (C5)", "pct_tins_with_verified_address"),
        ("Address sources used (C5)", "address_sources_used"),
        ("Best address vs NPPES practice address (C5)", "best_address_vs_nppes"),
        ("Rows in the provider rate file, confirmed PPO (C6)", "rate_file_rows"),
        ("... of which with an address", "rate_file_rows_with_address"),
        ("Rows in the candidate file, not confirmed (C6)", "candidate_file_rows"),
        ("Specialty source per NPI (C6)", "specialty_sources"), ("Providers scored (C7)", "providers_scored"),
        ("TINs scored (C7)", "tins_scored"), ("Cost tiers (C7)", "cost_tiers"),
        ("Candidate providers scored separately (C7)", "candidate_providers_scored"),
    ]
    lines = ["# Run summary", "", f"Generated {s['generated']} by `python -m ptd_run summary`.", "",
             "| Measure | Value |", "|---|---|"]
    for label, k in labels:
        v = s.get(k)
        if isinstance(v, dict):
            v = "; ".join(f"{a}: {b}" if not isinstance(b, dict) else f"{Path(str(a)).name}: {b}" for a, b in v.items())
        lines.append(f"| {label} | {'not run' if v in (None, '') else v} |")
    warnings = _input_warnings(c4, c5)
    if warnings:
        lines += ["", "## Inputs missing from this run", ""] + [f"- {w}" for w in warnings]
    s["input_warnings"] = warnings
    lines += ["", "## Deliverables", "",
              "| # | Deliverable | File |", "|---|---|---|",
              "| 1 | Provider rate file, confirmed PPO | `out/c6/c6_provider_rate_file.xlsx` (with cost columns: `out/c7/c7_provider_rate_file_scored.xlsx`) |",
              "| 1b | Candidate rates, not confirmed PPO | `out/c6/c6_provider_rate_file_candidates.xlsx` (scored on their own: `out/c7_candidates/`) |",
              "| 2 | Logic file: (a) PPO network, (b) TIN -> NPI -> address | `out/logic.md` |",
              "| 3 | File quality metrics | `out/file_quality_metrics.md` (scores: `out/c3/`) |",
              "| 4 | Summary file | `out/summary.md` (this file) |",
              "| 6 | Code repo | `pipeline/` (README.md) |",
              "| 7b | Cost-effectiveness metrics (C7) | `out/c7/c7_metrics.md`, `out/c7/c7_cost_effectiveness.xlsx` |", ""]
    (out / "summary.md").write_text("\n".join(lines))
    (out / "summary.json").write_text(json.dumps(s, indent=2, default=str))
    return s


def _copy_deliverables(out: Path) -> None:
    d = out / "deliverables"
    d.mkdir(exist_ok=True)
    write_logic(out)
    write_quality_metrics(out)
    for src, name in [("c6/c6_provider_rate_file.xlsx", "1_provider_rate_file.xlsx"),
                      ("c7/c7_provider_rate_file_scored.xlsx", "1_provider_rate_file_with_cost_scores.xlsx"),
                      ("c6/c6_provider_rate_file_candidates.xlsx", "1b_candidate_rates_not_confirmed.xlsx"),
                      ("c6/c6_network_check_sample.csv", "1c_network_check_sample.csv"),
                      ("c6/c6_network_accuracy.md", "1d_network_accuracy.md"),
                      ("logic.md", "2_logic_file.md"), ("file_quality_metrics.md", "3_file_quality_metrics.md"),
                      ("summary.md", "4_summary.md"),
                      ("c7/c7_metrics.md", "7b_cost_effectiveness_metrics.md"),
                      ("c7/c7_cost_effectiveness.xlsx", "7b_cost_effectiveness.xlsx")]:
        if (out / src).exists():
            shutil.copy(out / src, d / name)


def run_all(args) -> int:
    from ptd_c2.cli import main as c2
    from ptd_c3.cli import main as c3
    from ptd_c4.cli import main as c4
    from ptd_c5.cli import main as c5
    from ptd_c5.fetch import fetch
    from ptd_c6.cli import main as c6
    from ptd_c7.cli import main as c7
    from ptd_pipeline.__main__ import main as c1

    out, db = Path(args.out), str(Path(args.out) / "rates.sqlite")
    start = STAGES.index(args.from_stage)
    stop = STAGES.index(args.to_stage)
    if args.fetch_public:
        got = fetch(["nppes", "dac", "nucc"], Path(args.data) / "public")
        args.nppes = args.nppes or got.get("nppes")
        args.dac = args.dac or got.get("dac")
        args.nucc = args.nucc or got.get("nucc")
    as_of = ["--as-of", args.as_of] if args.as_of else []
    wb = ["--workbook", args.workbook] if args.workbook else []
    steps = {
        "c1": lambda: c1(["--html-file", args.html_file] if args.html_file else []),
        "c2": lambda: c2(["run", "--out", str(out / "c2")] + (["--no-probe"] if args.no_probe else [])),
        "c3": lambda: c3(["run", "--c2", str(out / "c2/c2_ppo_rate_files.csv"), "--out", str(out / "c3")] + as_of
                         + (["--expected-codes", args.workbook] if args.workbook else [])
                         + (["--files", *args.files] if args.files else [])),
        "c4": lambda: c4(["run", "--c2", str(out / "c2/c2_ppo_rate_files.csv"), "--c3", str(out / "c3/c3_file_scores.csv"),
                          "--db", db, "--out", str(out / "c4")] + wb + as_of
                         + (["--directory", args.directory] if args.directory else [])
                         + (["--directory-checks", *args.directory_checks] if args.directory_checks else [])
                         + (["--npis", *args.npis] if args.npis else [])
                         + (["--files", *args.files] if args.files else [])
                         + (["--workers", str(args.workers)] if args.workers else [])),
        "c3net": lambda: (c3(["run", "--c2", str(out / "c2/c2_ppo_rate_files.csv"), "--out", str(out / "c3_by_network"),
                              "--provider-tags", str(out / "c4/c4_provider_tags.csv")] + as_of
                             + (["--expected-codes", args.workbook] if args.workbook else [])
                             + (["--files", *args.files] if args.files else []))
                          if args.grade_by_network else 0),
        "c5": lambda: c5(["run", "--db", db, "--out", str(out / "c5")] + as_of
                         + sum(([f"--{k.replace('_', '-')}", str(getattr(args, k))] for k in
                                ("nppes", "nppes_pl", "dac", "directory", "zip_county") if getattr(args, k)), [])
                         + (["--npi-api", str(args.npi_api)] if args.npi_api is not None else [])),
        "c6": lambda: c6(["run", "--db", db, "--out", str(out / "c6")] + wb + (["--nucc", args.nucc] if args.nucc else [])),
        "c7": lambda: (c7(["run", "--db", db, "--out", str(out / "c7"), "--c6-out", str(out / "c6")] + wb
                          + (["--network-tags", *args.network_tags] if args.network_tags else []))
                       or c7(["run", "--set", "candidates", "--db", db, "--out", str(out / "c7_candidates"),
                              "--c6-out", str(out / "c6")] + wb)),
    }
    for name in STAGES[start:stop + 1]:
        if name in ("c1", "c2") and args.files:
            continue  # explicit rate files: no index needed
        print(f"\n===== {name.upper()} =====", flush=True)
        code = steps[name]()
        if code not in (0, None):
            print(f"[run] stage {name} failed with exit code {code}; stopping", flush=True)
            write_summary(out, Path(args.data))
            return code
    write_summary(out, Path(args.data))
    _copy_deliverables(out)
    print(f"\n[run] done. Summary: {out / 'summary.md'}; deliverables: {out / 'deliverables'}", flush=True)
    return 0


def summary_cmd(args) -> int:
    s = write_summary(Path(args.out), Path(args.data))
    _copy_deliverables(Path(args.out))
    print(json.dumps(s, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ptd_run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("all", help="run Step 0 and C1 to C7 in order")
    a.add_argument("--from", dest="from_stage", default="c1", choices=STAGES)
    a.add_argument("--to", dest="to_stage", default="c7", choices=STAGES)
    a.add_argument("--workbook", help="HiLabs Rate Benchmarking Individual Weights workbook")
    a.add_argument("--files", nargs="+", help="use these rate files instead of C1/C2's list (skips C1, C2)")
    a.add_argument("--html-file", help="C1: saved copy of the Centene page")
    a.add_argument("--no-probe", action="store_true", help="C2: do not open rate files")
    a.add_argument("--directory", help="payer directory table (C4 network evidence, C5 addresses)")
    a.add_argument("--directory-checks", nargs="+", help="C4: ptd_dir check coverage logs, for c4_ppo_check.csv")
    a.add_argument("--npis", nargs="+", help="C4 onwards: only these NPIs (list, text file or CSV with an npi column)")
    a.add_argument("--nppes", help="C5: NPPES monthly ZIP / npidata CSV / state extract")
    a.add_argument("--nppes-pl", help="C5: NPPES Practice Location Reference File")
    a.add_argument("--dac", help="C5: CMS Doctors and Clinicians file")
    a.add_argument("--zip-county", help="C5: ZIP to county CSV")
    a.add_argument("--npi-api", help="C5: NPI Registry API: auto (default), always, off, or a number of lookups")
    a.add_argument("--nucc", help="C6: NUCC taxonomy CSV")
    a.add_argument("--network-tags", nargs="+", help="C7: benchmark only these network tags (e.g. PPO)")
    a.add_argument("--grade-by-network", action="store_true", help="re-run C3 split by C4's network status")
    a.add_argument("--fetch-public", action="store_true", help="download NPPES, CMS D&C and NUCC first")
    a.add_argument("--workers", type=int, help="C4 worker processes")
    a.add_argument("--as-of", help="date for expiry and recency (YYYY-MM-DD)")
    a.add_argument("--data", default="data")
    a.add_argument("--out", default="out")
    a.set_defaults(func=run_all)
    s = sub.add_parser("summary", help="write out/summary.md from existing outputs")
    s.add_argument("--data", default="data")
    s.add_argument("--out", default="out")
    s.set_defaults(func=summary_cmd)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
