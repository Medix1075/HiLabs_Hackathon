# Centene price transparency pipeline (C1 to C7)

This pipeline reads Centene's Transparency in Coverage files and finds the rate files and plans for Health Net's **California commercial group PPO network**. It grades the data quality of those rate files, pulls out the PPO providers' rates, works out where each NPI + TIN actually practises, writes the provider rate file and scores every provider's cost against similar providers.

| Stage | Package | What it produces |
|---|---|---|
| Step 0: scrape the index links | `ptd_pipeline/scraper.py` | `data/toc_urls.json` |
| C1: parse the index files | `ptd_pipeline/index_parser.py` | `data/index_table.csv`, `data/rate_files.csv`, `data/raw/` |
| C2: find the CA group PPO plans and files | `ptd_c2/` | `out/c2/` (classification, PPO rate-file list, logs) |
| C3: grade each PPO rate file's data quality | `ptd_c3/` | `out/c3/` (score per file, metric justifications, examples) |
| C4: tag PPO providers and extract clean rates | `ptd_c4/` | `out/rates.sqlite` (the rate store), `out/c4/` (network tags, extraction log) |
| C5: TIN → NPI → practice address | `ptd_c5/` | `out/c5/` (addresses with confidence, **logic file**) |
| C6: the provider rate file | `ptd_c6/` | `out/c6/c6_provider_rate_file.xlsx` (**deliverable 1**) |
| C7: cost effectiveness | `ptd_c7/` | `out/c7/` (scores, tiers, **metrics file**, scored rate file) |
| Runner + summary | `ptd_run/` | `out/summary.md` and `out/deliverables/` |

C2 imports C1, and C3 imports C2. C4 to C7 share one SQLite rate store (`out/rates.sqlite`): C4 writes the rates, C5 adds addresses, C6 adds specialties and the headline rate, and C7 reads all of it. Each stage reads the previous stage's outputs by default.

## Run it

```bash
cd pipeline
python3 -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Everything, C1 to C7, with the public provider files downloaded first:
python -m ptd_run all --fetch-public --workbook "Rate Benchmarking Individual Weights_Hackathon.xlsx"
pytest                                 # 90 offline tests
```

`ptd_run all` runs C1, C2, C3, C4, C5, C6 and C7 in order and then writes `out/summary.md` and copies the four deliverables into `out/deliverables/`:

| Deliverable | File |
|---|---|
| 1. Provider rate file (NPI, TIN, address, billing code, specialty, rate) | `1_provider_rate_file.xlsx`, plus `1_provider_rate_file_with_cost_scores.xlsx` |
| 2. Logic file (how TIN → NPI → address was mapped) | `2_logic_file.md` |
| 3. Cost-effectiveness metrics | `3_cost_effectiveness_metrics.md`, `3_cost_effectiveness.xlsx` |
| 4. Summary (data quality, coverage, findings) | `4_summary.md` |

Useful options: `--from c4 --to c7` reruns part of the chain on earlier outputs. `--files a.json.gz` skips C1 to C3 and uses the given rate files. `--grade-by-network` reruns C3 split by C4's PPO / non-PPO tags. `--nppes`, `--dac`, `--directory`, `--zip-county` and `--nucc` point at files you already have instead of `--fetch-public`. `--as-of 2026-10-17` fixes the date used for expiry and recency, so runs are reproducible.

You can also run the stages separately:

```bash
python -m ptd_pipeline                 # Step 0 + C1 -> data/
python -m ptd_c2 run                   # C2 on data/index_table.csv and data/raw/
python -m ptd_c2 run --no-probe        # C2 without reading rate files (offline)
python -m ptd_c3 run                   # C3 on out/c2/c2_ppo_rate_files.csv -> out/c3/
python -m ptd_c2 run --c1 other_table.xlsx --index other_raw_dir/   # C2 on any C1-shaped table
python -m ptd_c2 validate --sample out/c2/c2_validation_sample.csv  # score the hand-filled spot check
python -m ptd_c4 run --workbook weights.xlsx   # C4 on C2's file list -> out/rates.sqlite, out/c4/
python -m ptd_c5 fetch --what nppes dac nucc   # download the public provider files into data/public/
python -m ptd_c5 run --nppes data/public/<nppes zip> --dac data/public/<dac csv>   # C5 -> out/c5/
python -m ptd_c6 run --workbook weights.xlsx   # C6 -> out/c6/
python -m ptd_c7 run --workbook weights.xlsx   # C7 -> out/c7/
python -m ptd_run summary                      # out/summary.md + out/deliverables/
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

---

## How C4 to C7 fit together

```
C2 file list ──► C4 ──► out/rates.sqlite ◄── C5 (addresses) ◄── NPPES, CMS Doctors & Clinicians, directory
   C3 scores ──►  │        (one store)    ◄── C6 (specialty, headline rate) ◄── HiLabs workbook, NUCC
 HiLabs workbook ─┘                       ──► C7 (peer benchmarks, provider scores)
```

The rate file pairs a TIN with its NPIs, and nothing public does (NPPES hides EINs). So C4 keeps that pairing exactly as the file gives it, and every later stage adds columns to it rather than re-deriving it. All four stages write into one SQLite store. That keeps memory flat whatever the file size, lets each stage be rerun on its own, and lets you open the store and query any intermediate step.

Every rule, weight and threshold below is in `config/c4.yaml` to `config/c7.yaml`, with a comment saying why. Nothing is hardcoded in the code.

## C4: tag the PPO providers and extract clean rates

```bash
python -m ptd_c4 run --workbook weights.xlsx                 # C2's file list, workbook codes only
python -m ptd_c4 run --files a.json.gz --codes all           # any rate file, every billing code
python -m ptd_c4 run --directory data/directory.csv          # payer directory as network evidence
python -m ptd_c4 directory --fhir-base https://.../fhir      # build that directory from a Plan-Net FHIR API
```

### Reading a 700 MB file fast, in flat memory

- Remote files are downloaded once to `data/rate_files/` and read from there.
- `provider_references` and `in_network` are read with ijson's C backend, one item at a time (`ijson.items`, about twice as fast as an event loop).
- **Parallel by sharding.** `in_network` is split over N worker processes (default: cores − 1). Worker *k* takes every item whose position is *k* mod N, so no splitting of the file is needed. Each worker writes its own SQLite shard with no locks, and the shards are merged at the end with `ATTACH`. A separate process reads `provider_references` at the same time. The result does not depend on the number of workers (a test checks this).
- **Refsets.** Each `negotiated_rates` entry lists the provider references it applies to. The same list repeats across thousands of codes, so it is stored once as a *refset* (a hash of the sorted list), and each price row points at its refset. On the 385 MB benchmark this turned 267k price rows × their reference lists into 30k refsets.
- Only codes in the HiLabs workbook are kept (`--codes all` keeps everything). Every item is still read, because codes outside the workbook still carry network evidence.

### Which providers are PPO: tagging each provider reference

C2 found that all 1,312 California group plans point to one Health Net file whose `network_name` is blank, so PPO and HMO providers sit in one file with nothing saying which is which. A provider can also be in both networks, under two references with two prices. **So the tag belongs to the provider reference, not to the NPI.** Each reference collects weighted evidence for PPO or non-PPO:

| Evidence | Weight | What it says |
|---|---|---|
| `network_name` | 1.0 | The reference's own network name, mapped with C2's rules. Used whenever the field is filled in. |
| `file_label` | 0.9 | C2 says the whole file is a single PPO network. |
| `directory` | 0.9 | The reference's NPIs are listed in a PPO or non-PPO network in a payer directory (e.g. Centene's Plan-Net FHIR API, via `ptd_c4 directory`). |
| `capitation` | 0.8 | The reference is paid per member per month for some services. That is an HMO arrangement; PPO contracts are fee-for-service. |
| `dual_contract` | 0.5 | *Hypothesis:* one TIN in two references listing mostly the same NPIs at different prices, where the higher-paid one is PPO. |
| `propagation` | spread | *Hypothesis:* label spreading over the graph of references that are priced together. |

A directory lists an NPI, not a reference. When the NPI sits in only one reference, the listing is direct evidence for it. When it sits in several (the PPO and HMO contracts of one group), the listing is held back. It is given out **by exclusion** once some of those references are known to be on the other side ("listed as PPO, and its other reference is capitated, so it's this one"). Anything still unresolved at the end is split evenly as weak `directory_shared` evidence, which never makes a tag high confidence.

**The two hypotheses are tested before they are trusted**, the same rule C2 uses for plan names:

- `dual_contract` is scored on TIN pairs where both networks are already known. It is used only if its precision is at least 0.9 on at least 10 pairs.
- `propagation` builds a bipartite graph of references and refsets: two references are linked when they are paid the same price for the same code, which is how one contract looks in the file. Labels spread from the known references (numpy, 30 iterations). It is checked by **5-fold cross-validation**: every known reference is hidden once and must be recovered from the others. It is used only if at least 90% are recovered, with at least 20 tested and at least 5 known references on each side, so a graph with only one class can't pass trivially. Very large refsets (over 5,000 references) are left out of the graph, since they are catch-all lists.

A reference is tagged `PPO` or `NON_PPO` when one side has at least 65% of its evidence, otherwise `UNKNOWN`. Confidence is high when direct evidence (`network_name`, `file_label`, `directory`) gives at least 90%. **C4 keeps `PPO` and `UNKNOWN` references** (`include_tags`) and drops only references with evidence against them. With no evidence at all, nothing is thrown away, and every row carries its tag and confidence so you can filter later. `c4_hypotheses.csv` reports what each hypothesis scored and whether it was used, and `c4_network_tags.csv` gives each reference its evidence string.

`c4_provider_tags.csv` (NPI, TIN, tag) plugs straight into C3's `--provider-tags`. `ptd_run all --grade-by-network` uses it to grade the PPO part of the file on its own.

### One clean rate per NPI + TIN + code + price variant

A code can legitimately have several prices for one provider: professional vs institutional, inpatient vs outpatient, place of service, modifier, fee schedule vs negotiated. Those are **price variants** (`negotiated_type`, `billing_class`, `setting`, `service_code`, `billing_code_modifier`) and are never mixed. Within a variant:

1. Dropped: capitation rows, rates ≤ 0, rows past their expiration date (`9999-12-31` counts as evergreen) and exact repeats. Every drop is counted in `c4_extraction_log.csv`.
2. If the provider still has several rates for the same variant, the tie-break is (`tie_break` in the config):
   1. `network`: the rate from the reference with the strongest PPO tag;
   2. `expiry`: the latest expiration date;
   3. `support`: the rate listed by the most references;
   4. `lowest`: the lowest rate, as a last resort.

   The rule that decided is stored with each rate (`pick_rule`), so a reviewer can see how often each one fired.

**Signatures make this fast.** An NPI + TIN's set of references is its *signature*, and thousands of NPIs share one. The choice is made once per signature and code, and fanned out to the NPIs through the `c4_rates` view. On the benchmark, 141k NPI + TIN pairs collapse to 30k signatures and 3.4M choices, which expand to 15.9M NPI × TIN × code × variant rates without being written out.

TIN-only groups (no NPI) can't produce NPI rows. They are counted and logged.

### Outputs (`out/c4/` and `out/rates.sqlite`)

| File | Contents |
|---|---|
| `out/rates.sqlite` | The rate store. `c4_rates` (view) has every NPI × TIN × code × variant rate with its tag, pick rule, expiry and source file. |
| `c4_network_tags.csv` | Per reference: tag, confidence, PPO and non-PPO evidence, and the evidence string |
| `c4_provider_tags.csv` | NPI, TIN and tag, for C3's `--provider-tags` |
| `c4_hypotheses.csv` | What each hypothesis scored, and whether it was used |
| `c4_extraction_log.csv` | Rows dropped by reason, and how often each tie-break rule decided |
| `c4_results.xlsx`, `c4_run_summary.json`, `c4_run_log.txt` | Workbook with a sample of rates, counts, timings, log |
| `c4_rates.csv.gz` | Only with `rates_csv: true`. Every rate as CSV; off by default because it is large and C6 writes the deliverable anyway. |

### Speed (synthetic, 4-core container)

`tools/make_synthetic_rate_file.py` generates a Health Net-shaped file (refsets reused 70/30 between PPO and HMO-like lists, capitation on HMO lists only, codes taken from the HiLabs workbook):

| File | References | Codes | Price rows in scope | Time | Peak memory |
|---|---|---|---|---|---|
| 56 MB gz / 385 MB JSON | 40,000 | 3,000 (594 in the workbook) | 267,254 | 73 s | 434 MB |

Reading took about 10 seconds with 3 workers, tagging under a second and extraction about 50 seconds. That gives 15.9M NPI × TIN × code × variant rates for 115,632 NPIs and 10,036 TINs. Writing all 15.9M rows out as `c4_rates.csv.gz` would add about 160 seconds, which is why it is off by default. The real CA file is roughly 700 MB, so expect a few minutes plus the download. The benchmark has no directory, so tagging only marks the 9,578 capitated references as non-PPO and keeps the rest as `UNKNOWN`. **A payer directory is what turns `UNKNOWN` into `PPO`.**

## C5: which address each NPI + TIN practises at

```bash
python -m ptd_c5 fetch --what nppes dac nucc        # download into data/public/
python -m ptd_c5 extract-nppes --nppes data/public/NPPES_Data_Dissemination_....zip --state CA   # optional, smaller file
python -m ptd_c5 run --nppes ... --dac ... [--nppes-pl ...] [--directory ...] [--zip-county ...] [--npi-api auto|always|off|N]
```

NPPES is self-reported and mostly stale, so C5 treats every source as a witness with a known reliability and adds the one thing only the rate file knows: **which other NPIs bill under the same TIN.** The full write-up, with this run's numbers and the closest calls, is generated as `c5_logic.md` (deliverable 2).

1. **Gather candidates.** Each NPI's addresses come from NPPES (practice, mailing and the Practice Location Reference File), CMS Doctors & Clinicians (current Medicare enrolment, with the group's `org_pac_id`), the payer directory (with TIN when it has one), the [NPI Registry API](https://npiregistry.cms.hhs.gov/api-page), and the practice address of any Type 2 (organisation) NPI under the same TIN.
2. **Standardise.** A local USPS Publication 28 standardiser (`address.py`: suffixes, directionals, unit designators, ordinal numbers, ZIP+4 trimmed) makes "123 North Main Street, Suite 4" and "123 N MAIN ST STE 4" the same address. Addresses are compared on **site** (number + street + ZIP5), so two suites in one building are one practice site.
3. **Score each site** with a noisy-OR across independent source families: `p = 1 − Π(1 − best weight in family × recency)`. Sources in one family (all the NPPES variants) share their data, so only the best one counts. NPPES evidence decays with the record's age (half-life 3 years, floor 0.4). Weights: directory 0.75, CMS D&C 0.70, NPPES practice 0.45, mailing 0.10.
4. **Make it TIN-specific.**
   - *TIN consensus:* the share of the TIN's other NPIs placed at this site, each weighted by how strongly its own sources place it there (a fresh CMS address counts more than an old NPPES one). This boosts the site: `p ← 1 − (1 − p)(1 − 0.6 × share)`.
   - When most of the TIN practises at a site the NPI has no address for, that site becomes a candidate (`tin_consensus_only`). This is how NPIs with only a stale NPPES address still get the right one.
   - When the TIN clearly practises elsewhere and none of its NPIs share this site, the site most likely belongs to the NPI's other TIN, so it is halved.
   - *Group alignment:* CMS groups NPIs by `org_pac_id`, not TIN. When most of a TIN's NPIs share one `org_pac_id`, that group's addresses are boosted for the TIN and the NPI's other groups are discounted.
5. **Penalties:** out of state × 0.3, PO box × 0.1. NPIs NPPES marks deactivated keep their rates but get no address.
6. **Choose.** Up to 3 sites per NPI + TIN are kept: the best, plus any with confidence ≥ 0.5 and ≥ 75% of the best (doctors often work at several sites of one group). A site is **verified** when two independent families agree, when the payer directory lists it, or when confidence is ≥ 0.8.
7. **Region** for C7 comes from the county (`--zip-county`, e.g. HUD's ZIP-county file) or else from the ZIP's first three digits, mapped to California market areas in the config.

**NPI Registry API.** The API serves the NPPES record of one NPI live: name, entity type, taxonomies, status, practice and mailing address, and secondary practice locations. By default (`--npi-api auto`) C5 asks it for every NPI the NPPES file doesn't have, which means every NPI when no NPPES file is given. That way a run without the ~1 GB monthly ZIP still gets taxonomies (so C6 can assign specialties) and NPPES addresses. `--npi-api always` asks for every NPI even with a file, `off` never calls it, and a number caps the lookups. Calls run on 4 threads under one shared limit of 8 per second, retry on 429/5xx, and stop after 20 failures (e.g. when the API is blocked). Every answer, including "not found", is cached in `data/npi_api_cache.jsonl` for 30 days, so a rerun only asks for new NPIs. About 2,800 NPIs take roughly 6 minutes the first time. Set `PTD_NPI_API=off` for offline runs.

The summary compares the chosen address with the NPPES practice address, which shows how much the resolution changed. Every candidate and its score is in `c5_candidates.csv` for audit.

| File | Contents |
|---|---|
| `c5_addresses.csv` | NPI, TIN, rank, address, confidence, verified, sources, evidence, county/region |
| `c5_providers.csv` | Per NPI: name, type, taxonomies, Medicare specialty, deactivation |
| `c5_tin_sites.csv` | Each TIN's sites and how many of its NPIs practise there |
| `c5_candidates.csv` | Every candidate site with its score, for audit |
| `c5_logic.md` | **Deliverable 2**, the logic file |
| `c5_results.xlsx`, `c5_run_summary.json` | Workbook and counts |

If NPPES or CMS can't be downloaded, C5 still runs on whatever it has (even nothing), says so in the log, and leaves the address blank rather than guessing.

## C6: the provider rate file

`python -m ptd_c6 run --workbook weights.xlsx [--nucc nucc.csv]` writes **deliverable 1**: one row per **NPI + TIN + address + billing code**.

- **The first six columns are exactly the ones asked for:** NPI, TIN, Address, Billing Code, Specialty, Rate. Then come the details a reviewer needs to trust a row: code type and description, NPI type and name, the price variant, expiry, how many variants exist, address rank, confidence, verified flag and sources, city, ZIP, region, specialty code and source, whether the code is expected for that specialty, network tag and confidence, the rate's pick rule and the source file.
- **Headline rate.** When a code has several price variants, one is shown, with the variant count beside it. The preference is a dollar rate type first, then the billing class that fits the provider (Type 1 individuals: professional; Type 2 organisations: institutional), then outpatient, then place of service 11 (office), then no modifier. Every variant stays in the `c4_rates` view.
- **Specialty**, first match wins, with the source recorded:
  1. the primary NPPES taxonomy through the HiLabs workbook's taxonomy crosswalk;
  2. a secondary taxonomy through the same crosswalk;
  3. the CMS Medicare specialty, matched to the workbook's specialties by name;
  4. **inferred from the codes the provider bills**: the workbook specialty whose code profile best matches, by F-score of coverage and precision (needs at least 3 workbook codes);
  5. the NUCC name for the taxonomy;
  6. otherwise `Unmapped`.
- The Excel is written in streaming mode (flat memory), split into sheets of 1M rows, and stops at 3M rows. `c6_provider_rate_file.csv.gz` always has every row. An "About" sheet explains each column.

## C7: cost effectiveness against similar providers

`python -m ptd_c7 run --workbook weights.xlsx` writes **deliverable 3** (`c7_metrics.md`, generated with this run's numbers) and the scores.

1. **Like for like.** A rate is only compared with rates for the same billing code *and* the same price variant (rate type, billing class, setting, place of service, modifier). Percentage-of-charges rates are left out, since the charge is unknown.
2. **Similar providers, with back-off.** Each rate is benchmarked against the most specific peer group that has at least 5 providers: specialty + region + NPI type, then specialty + type statewide, then type, then everyone. The level used is recorded on every row, so a reviewer can see how local each comparison is.
3. **Per rate:** the peer median, P25 and P75, the rate's percentile among its peers (mid-rank, so ties are fair), and the **rate index** = rate ÷ peer median. This is vectorised with numpy over all groups at once.
4. **Per provider (NPI + TIN):** the **cost index** is a weighted geometric mean of the rate indexes. A geometric mean is used because the indexes are ratios: one rate at 2× and one at 0.5× should average to 1.0. Each code is weighted by its `SPEC_PROC_WEIGHT` for the provider's specialty in the HiLabs workbook, so 99214 counts far more than a rare code.
5. **Shrinkage.** A provider with one cheap code isn't proven cheap. The log index is multiplied by `n_eff / (n_eff + 2)`, where `n_eff` is Kish's effective number of codes (empirical Bayes towards the market index of 1.0). The raw index is kept beside the shrunk one.
6. **Score and tier.**
   - The **cost score (0–100)** is the provider's percentile among providers of the same specialty and region (falling back to specialty, then everyone, below 10 providers). 100 means the cheapest.
   - **Tiers** come from the shrunk index: Highly cost-effective ≤ 0.85, Cost-effective ≤ 0.95, At market ≤ 1.05, Above market ≤ 1.20, High cost above that.
   - **Confidence** is high with `n_eff` ≥ 8 and at least 30% of the specialty's workbook weight covered, medium with `n_eff` ≥ 3, otherwise low.
7. **Per TIN:** the members' shrunk indexes averaged on the log scale, each weighted by its `n_eff`, for group-level contracting views. `--network-tags PPO` benchmarks only rates C4 tagged PPO.

| File | Contents |
|---|---|
| `c7_provider_scores.csv` | Per NPI + TIN: cost score, cost index (shrunk and raw), tier, confidence, n_eff, codes, coverage |
| `c7_tin_scores.csv` | Per TIN |
| `c7_rate_benchmarks.csv.gz` | Every rate with its peer level, peer count, median, P25/P75, percentile and index |
| `c7_cost_effectiveness.xlsx` | Providers sorted by score, TINs, and how many rates used each peer level |
| `c7_provider_rate_file_scored.xlsx` / `.csv.gz` | The C6 rate file with the cost columns added |
| `c7_metrics.md` | **Deliverable 3**, the metrics file |

## What has and hasn't been checked

- **Public sources degrade gracefully.** NPPES, CMS Doctors & Clinicians and NUCC are discovered from their download pages, not hardcoded. If they are unreachable, the stage logs it and continues without them.
- **Not yet run on the real Health Net file.** centene.com, CMS and NPPES were not reachable from the build machine. C4 to C7 have been verified only on synthetic data:
  - a California world built to contain the traps (`tests/ca_world.py`): PPO and HMO contracts sharing NPIs, capitation, stale and deactivated NPPES records, an NPI under two TINs, TIN-only groups, junk variants;
  - the 385 MB benchmark above.

  On the synthetic world, no reference gets the wrong network tag, every NPI + TIN's rank-1 address is its true site, and the C7 cost index rises with each provider's true price tier.
- **The PPO split needs a directory.** Without a payer directory (or filled-in `network_name`), the only hard evidence in the CA file is capitation. That removes HMO references but cannot confirm PPO ones, so most rows stay `UNKNOWN` and are kept. Run `python -m ptd_c4 directory --fhir-base <Centene Plan-Net FHIR URL>` from a machine that can reach it, and pass the result with `--directory`.

## Tests

`pytest` runs 90 offline tests in about 6 seconds.

- **C1 (9 tests):** scraper structure on a saved page, employee exclusion, date parsing, flattening, dedup, gzip.
- **C2 (41 tests):** a miniature Centene world served over local HTTP. It covers gzip, `network_name` as string, list or blank, `in_network` before `provider_references`, `location` references, mixed and unreachable files, the scope traps, the EnhancedCare flag, and the real EOA/EC/CUSPPO name shapes.
- **C3 (14 tests):** hand-built clean and dirty rate files with every problem counted in advance, NPI check digits, plain and weighted code coverage (a workbook in the HiLabs layout), provider tags (including references after `in_network`), `location` references, byte-cap sampling, unreachable files and the CLI.
- **Integration (1 test):** `--run-c1` going from a saved page through C1 into C2.
- **C4 to C7 (25 tests):** one synthetic California world (`tests/ca_world.py`) run through C4, C5, C6, C7 and the summary, with the truth known in advance:
  - C4: no reference gets the wrong network tag; HMO-only and capitated references are excluded; the hypotheses are gated; variants are never mixed; the tie-break picks the right rate; code scope works; the result is the same with 1 or 3 workers and with `in_network` before or after `provider_references`.
  - C5: every NPI + TIN's rank-1 address is its true site, including the NPI under two TINs, stale NPPES records, deactivated NPIs and NPIs missing from NPPES. Also the standardiser, the logic file and a run with no sources.
  - C6 and C7: the required column order, headline variant and specialty source; the cost index rises with the true price tier; benchmark statistics; the metrics file and scored rate file.
  - The Plan-Net FHIR crawler, the NPI Registry API (as a fallback for missing NPIs, as the only source with its cache reused on a rerun) and the NPPES extractor, against local HTTP servers.

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
├── config/c4.yaml … c7.yaml  # every C4-C7 weight, threshold and rule, with the reason for each
├── ptd_c4/                 # C4: python -m ptd_c4 run | directory
│   ├── ingest.py           # sharded parallel streaming into SQLite
│   ├── network.py          # PPO / non-PPO evidence, hypotheses, label spreading
│   ├── directory.py        # payer directory loader and Plan-Net FHIR crawler
│   ├── extract.py          # signatures, price variants, tie-break
│   └── db.py  config.py  cli.py
├── ptd_c5/                 # C5: python -m ptd_c5 run | fetch | extract-nppes
│   ├── address.py          # USPS Pub 28 standardiser
│   ├── sources.py          # NPPES, practice locations, CMS D&C, directory, NPI API readers
│   ├── resolve.py          # scoring, TIN consensus, group alignment, selection
│   ├── fetch.py            # public file discovery and download
│   └── logic.py  config.py  cli.py   # logic.py writes deliverable 2
├── ptd_c6/                 # C6: python -m ptd_c6 run
│   └── specialty.py  cli.py
├── ptd_c7/                 # C7: python -m ptd_c7 run
│   ├── bench.py            # peer groups with back-off, vectorised statistics
│   ├── score.py            # provider and TIN indexes, shrinkage, scores, tiers
│   └── report.py  cli.py   # report.py writes deliverable 3
├── ptd_run/                # python -m ptd_run all | summary (deliverable 4)
├── tools/make_synthetic_rate_file.py   # large Health Net-shaped test files
├── tests/
└── data/, out/             # generated, git-ignored
```

Context: Health Net is ending California commercial group plans, with most policies ending 2027-02-28. This is logged in every C2 run.
