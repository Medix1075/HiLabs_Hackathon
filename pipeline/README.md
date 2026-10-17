# Centene price transparency pipeline (C1 + C2 + C3)

This pipeline reads Centene's Transparency in Coverage files and finds the rate files and plans for Health Net's **California commercial group PPO network**.

| Stage | Package | What it produces |
|---|---|---|
| Step 0: scrape the index links | `ptd_pipeline/scraper.py` | `data/toc_urls.json` |
| C1: parse the index files | `ptd_pipeline/index_parser.py` | `data/index_table.csv`, `data/rate_files.csv`, `data/raw/` |
| C2: find the CA group PPO plans and files | `ptd_c2/` | `out/c2/` (classification, PPO rate-file list, logs) |
| C3: grade each PPO rate file's data quality | `ptd_c3/` | `out/c3/` (score per file, metric justifications, examples) |

C2 imports C1, and C3 imports C2. Each stage reads the previous stage's outputs by default, and `--run-c1` / `--run-c2` run the earlier stages first.

## Run it

```bash
cd pipeline
python3 -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m ptd_c3 run --run-c2 --run-c1 # Step 0 + C1 + C2 + C3 in one go
pytest                                 # 65 offline tests
```

You can also run the stages separately:

```bash
python -m ptd_pipeline                 # Step 0 + C1 -> data/
python -m ptd_c2 run                   # C2 on data/index_table.csv and data/raw/
python -m ptd_c2 run --no-probe        # C2 without reading rate files (offline)
python -m ptd_c2 run --c1 other_table.xlsx --index other_raw_dir/   # C2 on any C1-shaped table
python -m ptd_c2 validate --sample out/c2/c2_validation_sample.csv  # score the hand-filled spot check
```

C1 options: `--brand "Health Net"` processes only one brand (repeatable). `--no-cache` streams index files without saving them. `--refresh` re-downloads cached index files. `--html-file page.html` parses a saved copy of the Centene page. `-v` turns on debug logging.

---

## Step 0: scrape the index links

Source: <https://www.centene.com/price-transparency-files.html>

- The scraper finds the heading **"Links to the download TOC files"** and collects every `<a href>` after it, up to the next heading ("Centene Employees"). Employee links are excluded by page structure, not by name. That matters because the employee section also has a "HEALTH NET" link.
- It keeps only `.json` / `.json.gz` links and turns relative paths into absolute URLs.
- The **brand** comes from the link label ("Index File for **Health Net** Plans"). QualChoice sub-indexes inherit their parent's brand, and the manifest records `is_sub_index` and `parent_url`.
- **No hardcoded JSON URLs or dates.** The `YYYY-MM-DD` prefix is read from each file name, and the tests use a fixture dated `2099-…` to prove it.
- If the heading or its links can't be found, the scraper stops with an error instead of picking up the wrong links.
- **MHN** has no index file. Its plans are inside Ambetter CA (marketplace) and Health Net (commercial).

## C1: parse the index files

Every index follows the CMS table-of-contents schema. Each `reporting_structure` produces one row per (`reporting_plans` × `in_network_files`) pair:

| Column | Source field |
|---|---|
| `brand` | From Step 0 |
| `plan name` | `reporting_plans[].plan_name` |
| `plan ID` | `reporting_plans[].plan_id` |
| `market type` | `reporting_plans[].plan_market_type` |
| `rate file URL` | `in_network_files[].location` |

- **Deduplication:** rows go into SQLite (`data/ptd.sqlite`) with a `UNIQUE` constraint on all five columns and `INSERT OR IGNORE`. A repeated row is dropped whether it comes from another reporting structure or from an overlapping index (QualChoice parent vs. sub-index). Distinct plans that share one rate file are kept. `data/rate_files.csv` lists each unique rate file once, with its plan count.
- **Scalability:** `ijson` reads one `reporting_structure` at a time. Downloads stream to `data/raw/` in 1 MiB chunks through a `.part` file. The dedup set lives in a SQLite index, not in memory. Gzip is detected by magic bytes, and HTTP retries back off on 429/5xx.
- `allowed_amount_file` (out-of-network) links are not rate files, so C1 leaves them out. C2 reads them straight from the cached raw indexes in `data/raw/`.

**Checked on the 2026-09-28/30 files:** all 11 index files (4 brands + 7 QualChoice sub-indexes) were parsed. 3,275 plan × file pairs became 3,270 unique rows after removing 5 duplicates, across 33 unique rate files. An independent `json.load` of every raw index produced exactly the same 3,270 rows.

## C2: find the California commercial group PPO plans and files

C2 implements the seven steps of the C2 brief:

| Step | Module | What it decides |
|---|---|---|
| 1 Scope | `scope.py` | Keeps `group` market, Health Net, state CA. State comes from the plan-name prefix (`CA L ...`), a HIOS id (`67138CA060`), the file name (`..._healthnet-or_in-network`), or the brand's home state as a flagged fallback. The word "PPO" plays no part, so Ambetter `Gold PPO` falls to the market rule and QualChoice `PPO Complete` to the brand and state rules. |
| 2 Name signals | `name_signals.py` | Tags `PPO/HMO/EPO/POS/HSP/EOA` keywords and product codes (`SC`, `SAL`, `WholeCare`, `CommunityCare`, `Full Network`, `PureCare`). `EC` counts as EnhancedCare PPO only when no other type is present. Names with two types become `conflict`. The hypothesis signals (`L&T`/`Life` hint, benefit-string shape) are tested on plans with explicit keywords and used only if they reach ≥90% precision on ≥10 plans. |
| 3 File vote | `file_vote.py` | Groups plans by rate file and labels the file `PPO` or `NON_PPO` at ≥80% share (configurable), otherwise `MIXED` or `NO_VOTES`. A plan alone in its file doesn't vote for itself. `plan_id` is never used as a key. |
| 4 network_name | `network_probe.py` | Streams the start of each in-scope rate file (gzip handled) and reads `provider_references[].network_name`, the TIN/NPI spread, and `location` references. Names are mapped to `PPO_FULL`, `PPO_ENHANCEDCARE`, `HMO`, `HSP`, `EPO` or `POS` by config rules. Blank names are counted, not treated as names. A file with both PPO and non-PPO names is `MIXED`, and its PPO network names are passed on for C4 to filter on. |
| 5 Supporting | `supporting.py` | `oon_signal` (whether the plan's `allowed_amount_file` has records) and `payment_signal` (ffs vs capitation share, TIN spread). Each is tested against network_name-confirmed rows in the same run and adjusts confidence only if it agrees ≥90% of the time. Neither ever changes a label. |
| 6 Combine | `combine.py` | Priority: network_name > file vote > plan name > supporting. **High** needs network_name with no disagreement. **Medium** covers a file vote backed by other plans, or an explicit plan-name keyword or product code when the file is shared. **Low** covers hints, shapes and unresolved conflicts. Every disagreement goes to the conflicts log. |
| 7 Validation | `validation.py` | Writes a seeded random sample to check by hand against Health Net Summary of Benefits documents, and scores agreement once it is filled in. It never scrapes. |

### Outputs (`out/c2/`)

| File | Contents |
|---|---|
| `c2_classification.csv` | One row per C1 row, out-of-scope rows included, with `final_network_type`, `ppo_variant`, `in_scope_ppo`, `needs_network_filter`, `confidence`, `decided_by` and a one-line `reason` |
| `c2_ppo_rate_files.csv` | **Hand-off to C3/C4:** distinct in-scope PPO rate files, PPO network names to filter on, confidence |
| `c2_exclusions.csv` | Every row removed by Step 1, with the rule(s) it failed |
| `c2_name_signals.csv`, `c2_file_votes.csv`, `c2_network_probe.csv`, `c2_supporting.csv` | Per-step evidence |
| `c2_conflicts.csv` | Every conflict and how it was resolved |
| `c2_hypotheses.csv` | Measured result for each hypothesis, and whether it was used |
| `c2_validation_sample.csv` | Step 7 sheet to fill in |
| `c2_results.xlsx`, `c2_run_summary.json`, `c2_run_log.txt` | Workbook of the main tables, counts, log |

### What C2 finds on the real 2026-09-30 files

From `python -m ptd_c2 run --no-probe` on C1's output. Rate files were not reachable from the build machine.

- **Scope:** 1,312 of 3,270 rows are kept. Excluded rows: 1,214 not group market, 363 not Health Net, and 381 Oregon. Health Net also publishes `healthnet-or_in-network.json`, which the file-name state rule catches. 255 of the excluded rows had "PPO" in the name.
- **All 1,312 CA group plans point to one rate file**, `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json` (~700 MB), and one allowed-amounts file. The file vote is therefore `MIXED`, and the OON signal can't tell plans apart.
- **Plan types:** 453 PPO, 723 HMO, 127 EOA (Elect Open Access), 6 POS. Three PPO+EPO conflicts (Ironworkers ×2, Sacramento City USD) are left `UNKNOWN`, out of scope, and listed in the conflicts log.
- **network_name is blank.** A sample of the CA file's first 1.7 MB showed all 2,825 provider groups with an empty `network_name`, so the strongest signal is missing. The PPO network can't be separated from the HMO networks inside the file by network name. That is why the CA file is flagged `needs_network_filter = True`, and C4 will need another way to tell PPO providers apart.
- **Real-name lessons now in the config:** `EOA` is a non-PPO type. `CUSPPO` (missing space) is PPO. `EC` never stands alone: all 140 EC plans sit next to HMO or EOA.

### Decisions you can flip in `config/c2.yaml`

- **EnhancedCare PPO counts as PPO** by default (`include_enhancedcare_ppo`). Rows are tagged `ppo_variant` FULL or ENHANCEDCARE so C3/C4 can split it out.
- **POS and EOA are not PPO.** Both count against PPO in the file vote.
- Plans with no state in their name, id or file name fall back to the brand's home state and are flagged. `require_explicit_state: true` drops them instead.
- All thresholds, keyword lists and network-name rules are in the config. No URLs or dates are hardcoded.

---

## C3: grade each rate file's data quality

`python -m ptd_c3 run` reads C2's hand-off (`out/c2/c2_ppo_rate_files.csv`) and streams every listed rate file once, start to end. Nothing is loaded whole: ijson builds one `provider_references` item or one `in_network` item at a time, measures it and drops it. Memory grows with the number of provider groups and distinct codes, not with file size. On a 61 MB synthetic file (240,000 price rows) a run takes 8 seconds and 80 MB of memory, about 7.5 MB/s, so the ~700 MB Health Net CA file takes a couple of minutes plus download time.

```bash
python -m ptd_c3 run                                   # grade C2's PPO rate files
python -m ptd_c3 run --files a.json.gz https://.../b.json   # grade any files
python -m ptd_c3 run --expected-codes "Rate Benchmarking Individual Weights_Hackathon.xlsx"  # code coverage
python -m ptd_c3 run --provider-tags tags.csv          # also grade each provider tag (e.g. PPO only)
python -m ptd_c3 run --max-bytes 200000000             # quick sample of each file, flagged "sampled"
python -m ptd_c3 run --as-of 2026-10-17                # fixed grading date, for reproducible runs
```

### The nine metrics

Each metric gives a raw value, a 0-1 score and a one-line justification (`c3_metrics.csv`). "Share of bad rows" metrics score `1 - min(1, share / tolerance)`, so a share at or above its tolerance scores 0. Tolerances and weights are in `config/c3.yaml`. The unit is one price row (one `negotiated_prices` entry).

| Metric | Weight | What it measures |
|---|---|---|
| `schema_validity` | 15 | Problems per record against CMS v2.0.0: required fields, enums (`negotiation_arrangement`, `negotiated_type`, `billing_class`, `setting`, `tin.type`, `billing_code_type`), code formats (CPT, HCPCS, MS-DRG, RC, CDT, NDC), date format, `service_code` on professional rows, bundles without codes, references to undefined `provider_group_id`s, blank `network_name`. Halved if a top-level field is missing. |
| `code_coverage` | 15 | Coverage of the codes in HiLabs' *Rate Benchmarking Individual Weights* workbook (`--expected-codes`; 583 CPT/HCPCS codes over 54 specialties). Each code is weighted by specialty weight × its share of that specialty's code weight, and the weighted share is the score, so missing 99214 (13% of the weight) costs far more than missing a rare code. The plain share is in the justification. Six facility specialties (Urgent Care, Skilled Nursing, ...) have no specialty weight, so they count in the plain share and the per-specialty sheet but not in the score. Without `--expected-codes` the metric is "not measured" and its weight is spread over the rest. Any CSV/XLSX with a code column also works, unweighted. |
| `provider_coverage` | 15 | Share of price rows tied to at least one provider group with a valid NPI (10 digits and a correct check digit). Also counts distinct NPIs and TINs. California-only counts need the NPPES extract (C5). |
| `zero_rates` | 10 | Rows with a rate of 0 or less. |
| `outliers` | 10 | Rates outside a log-IQR fence (k = 3) **and** at least 3x from the median of rates for the same code, `negotiated_type`, `billing_class`, `setting` and modifiers, plus percentages above 100. Those splits are legitimately different prices, so they are never compared with each other. Groups under 5 rates are not tested. |
| `rate_type_mix` | 10 | Share of rows that are dollar amounts comparable across providers (`negotiated`, `fee schedule`, `derived`). The full mix, per diem and percentage included, is in the justification. |
| `duplicates` | 10 | Rows repeating an earlier row exactly (same code, providers, type, rate, class, setting, service codes, modifiers, expiry). File-wide up to 5M rows, then per item, and the output says which. |
| `staleness` | 10 | Average of the expired-row score (`9999-12-31` counts as evergreen, not expired) and a file-age score from `last_updated_on` (1 up to 45 days, 0 at 365). |
| `junk` | 5 | TIN-only groups (`npi: []` or `[0]`), placeholder TINs (`999999999`, ...), NPIs failing the check digit, and rows on catch-all codes (`CSTM-ALL`, `CSTM-00`). |

The final score is the weighted mean of the measured metrics on a 0-100 scale, graded A (90+), B (80+), C (70+), D (60+) or F.

### The mixed Health Net file, and provider tags

C2 found that all 1,312 CA group plans share one rate file whose `network_name` is blank, so PPO providers can't be separated from HMO providers by network name (`needs_network_filter = True`). **C3 therefore grades each file as a whole** (bucket `ALL`) and says so in the log. The blank `network_name` on every reference shows up as a schema problem.

The hook for later: `--provider-tags` takes a CSV/XLSX with `npi` and/or `tin` and a `tag` column. In the same pass, every price row is also counted under the tags of the provider groups it references, and each tag gets its own score row (e.g. `PPO`, `HMO`, `untagged`). When a file puts `in_network` before `provider_references`, C3 reads the references first so the tags are known.

### Outputs (`out/c3/`)

| File | Contents |
|---|---|
| `c3_file_scores.csv` | One row per file and bucket: score, grade, each metric's score, rows, codes, NPIs, TINs, status (`ok`, `sampled` or `error`), and C2's `needs_network_filter` and network columns |
| `c3_metrics.csv` | One row per file, bucket and metric: raw value, score, weight, justification |
| `c3_schema_problems.csv` | Count of each schema problem per file |
| `c3_examples.csv` | Up to 5 example rows per issue per file (zero rates, outliers, duplicates, expired, TIN-only groups, schema problems) |
| `c3_code_coverage.csv` | Only with `--expected-codes`: plain and weighted coverage per specialty, with missing codes |
| `c3_results.xlsx`, `c3_run_summary.json`, `c3_run_log.txt` | Workbook, counts, log |

Rate files couldn't be reached from the build machine, so C3 has been run on synthetic files only. Run it locally on the real Health Net file.

## Tests

`pytest` runs 65 offline tests.

- **C1 (9 tests):** scraper structure on a saved page, employee exclusion, date parsing, flattening, dedup, gzip.
- **C2 (41 tests):** a miniature Centene world served over local HTTP. It covers gzip, `network_name` as string, list or blank, `in_network` before `provider_references`, `location` references, mixed and unreachable files, the scope traps, the EnhancedCare flag, and the real EOA/EC/CUSPPO name shapes.
- **C3 (14 tests):** hand-built clean and dirty rate files with every problem counted in advance, NPI check digits, plain and weighted code coverage (a workbook in the HiLabs layout), provider tags (including references after `in_network`), `location` references, byte-cap sampling, unreachable files and the CLI.
- **Integration (1 test):** `--run-c1` going from a saved page through C1 into C2.

## Layout

```
pipeline/
├── config/c2.yaml          # every C2 threshold, keyword and decision
├── config/c3.yaml          # every C3 tolerance, weight and rule
├── ptd_pipeline/           # Step 0 + C1
│   ├── __main__.py         # python -m ptd_pipeline
│   ├── scraper.py
│   └── index_parser.py
├── ptd_c2/                 # C2 (imports ptd_pipeline)
│   ├── cli.py              # python -m ptd_c2 run | validate
│   ├── scope.py  name_signals.py  file_vote.py  network_probe.py
│   ├── supporting.py  combine.py  validation.py
│   └── loaders.py  normalize.py  stream.py  config.py
├── ptd_c3/                 # C3 (imports ptd_c2)
│   ├── cli.py              # python -m ptd_c3 run
│   ├── scan.py             # the streaming pass
│   ├── metrics.py          # scores and justifications
│   └── tags.py  codes.py  config.py
├── tests/
└── data/, out/             # generated, git-ignored
```

Context: Health Net is ending California commercial group plans, with most policies ending 2027-02-28. This is logged in every C2 run.
