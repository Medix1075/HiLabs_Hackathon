# HiLabs Hackathon: Centene PPO price transparency pipeline (C0 to C7)

This repository reads Centene's public price transparency files and turns them into one Excel file a contracting team can use: **one row per NPI, TIN, practice address and billing code, with the specialty and the negotiated rate**, for Health Net's **California commercial group PPO network**. On the way it grades how good the files are, works out which providers really are in the PPO network, finds where each provider actually practises (NPPES alone is stale), and scores every provider's cost against similar providers.

Everything runs with one command, streams the 700 MB rate file instead of loading it, and writes all the hackathon deliverables into `pipeline/out/deliverables/`.

- [1. Results right now](#1-results-right-now)
- [2. Inputs and outputs](#2-inputs-and-outputs)
- [3. The C0 to C7 flow](#3-the-c0-to-c7-flow)
- [4. Each step: flowchart, rules, fixed values and metrics](#4-each-step-flowchart-rules-fixed-values-and-metrics)
- [5. How to run it](#5-how-to-run-it)
- [6. How long each step takes, and the bottlenecks](#6-how-long-each-step-takes-and-the-bottlenecks)
- [7. Known limits](#7-known-limits)
- [8. Repository layout](#8-repository-layout)

A longer write-up of how we got here (ideas we tried and dropped, edge cases, what to do next) is in the Word documentation that goes with this README.

---

## 1. Results right now

Full run on **2026-10-18** (`--as-of 2026-10-18`), on the real Centene files dated 2026-09-28/30, reusing the Health Net PPO site check as it stood at 12:08 UTC: **2,603 of the rate file's 3,734 NPIs searched** (2,630 NPI + TIN rows). The search of the remaining NPIs was still running, so these numbers will grow when it finishes.

| Measure | This run (2,603 NPIs searched) | Previous run (1,703 NPIs searched) |
|---|---|---|
| Index files found and parsed (Step 0, C1) | 11 of 11 | 11 of 11 |
| Plan x rate-file rows / unique rate files (C1) | 3,270 / 33 | 3,270 / 33 |
| California commercial group plans (C2) | 1,312 | 1,312 |
| PPO plans / PPO rate files (C2) | 453 / 1 | 453 / 1 |
| Plan types (C2) | PPO 453, HMO 723, EOA 127, POS 6, unknown 3 | same |
| File quality score (C3) | **84.3 / 100, grade B** | 84.3, B |
| Rate file shape (C4) | 701.6 MB, 2,825 provider references, 1.5M price rows, 775 codes, 134 of 583 workbook codes | same |
| NPI + TIN pairs: confirmed PPO / candidate / unresolved / non-PPO (C4) | **557** / 1,768 / 1,440 / 10 | 376 / 1,688 / 1,700 / 11 |
| Rates extracted, NPI x TIN x code x variant (C4) | 4,826 (1,929 NPIs, 1,550 TINs) | 3,270 (1,268 NPIs, 1,053 TINs) |
| NPIs linked to an address (C5) | **99.81%** | 99.71% |
| NPIs linked to a **verified** address (C5) | **36.13%** | 34.75% |
| TINs linked to an address / verified address (C5) | 99.95% / 35.71% | 99.93% / 34.76% |
| Best address differs from NPPES's own address (C5) | 234 of 2,617 pairs | 154 of 1,712 |
| **Deliverable 1** rows, confirmed PPO (C6) | **1,155 rows**: 185 NPI + TIN pairs, 117 TINs, 99 codes, all with a verified address | 874 rows |
| Candidate file 1b rows, not confirmed (C6) | 3,739 | 2,447 |
| Providers / TINs scored for cost, confirmed set (C7) | 135 / 81 | 66 / 39 |
| Cost tiers, confirmed set (C7) | Highly cost-effective 13, Cost-effective 7, At market 98, Above market 11, High cost 6 | |
| Candidate providers scored separately (C7) | 1,639 | 1,037 |

What the numbers say, in plain words:

- **The PPO network is one file.** All 1,312 California group plans (PPO and HMO alike) point to the same Health Net rate file, and that file leaves `network_name` blank on all 2,825 provider groups. So the file can't tell you which provider is PPO. We answer that with Health Net's own provider finder (section 4, C4).
- **Deliverable 1 only holds pairs that are proven PPO by their own listing.** Everything else is kept in the candidate file 1b with its status and reason, so nothing is lost and nothing unproven is presented as PPO.
- **The file prices few of HiLabs' codes.** Only 134 of the workbook's 583 codes appear, so most providers have 2 or 3 priced codes and most cost scores sit near "At market".
- **Address coverage is almost complete, and a third is verified by a second source.** It started at 11% verified on the first real run.

The run's own files are in the project folder `research/directory/pipeline-2603npis-2026-10-18/` (summary, all deliverables, the frozen PPO check CSV and the run log).

| # | Deliverable | File in `pipeline/out/deliverables/` |
|---|---|---|
| 1 | Provider rate file (Excel): NPI, TIN, Address, Billing Code, Specialty, Rate, confirmed PPO only | `1_provider_rate_file.xlsx`, `1_provider_rate_file_with_cost_scores.xlsx` |
| 1b | Rates not confirmed PPO, kept for review | `1b_candidate_rates_not_confirmed.xlsx`, `1c_network_check_sample.csv`, `1d_network_accuracy.md` |
| 2 | Logic file: (a) PPO network, (b) TIN to NPI to address | `2_logic_file.md` |
| 3 | File quality metrics | `3_file_quality_metrics.md` |
| 4 | Summary | `4_summary.md` |
| 5 | Video | not in the repo |
| 6 | Code repo, re-runs from this README | this repository |
| 7b | Cost-effectiveness metrics (C7) | `7b_cost_effectiveness_metrics.md`, `7b_cost_effectiveness.xlsx` |

---

## 2. Inputs and outputs

### Inputs

| Input | Where it comes from | Used in | Needed? |
|---|---|---|---|
| Centene price transparency page and its 11 index files | https://www.centene.com/price-transparency-files.html (found by the scraper, never typed in) | Step 0, C1 | Yes |
| Health Net CA in-network rate file (701.6 MB) and allowed-amount file | URLs inside the Health Net index | C2 to C4 | Yes |
| HiLabs workbook *Rate Benchmarking Individual Weights_Hackathon.xlsx* (taxonomy crosswalk, specialty weights, 946 specialty x code weights over 583 codes) | Given by HiLabs; put it in `pipeline/inputs/` | C3, C4, C6, C7 | Strongly recommended |
| `c4_ppo_check.csv`: Health Net Find a Provider result per NPI + TIN | Made by C4 `--check-directory` (findaprovider.healthnetcalifornia.com), or a saved copy | C4, C5 | Needed to confirm any PPO provider |
| NPPES monthly file (1.16 GB zip, includes the Practice Location file) | CMS, downloaded by `--fetch-public` | C5, C6 | Recommended |
| CMS Doctors and Clinicians file (900 MB) | CMS, downloaded by `--fetch-public` | C5 | Recommended |
| NPI Registry API | npiregistry.cms.hhs.gov, live, cached 30 days | C5 (NPIs missing from NPPES), site check (city per NPI) | Automatic |
| US Census Bureau geocoder | geocoding.geo.census.gov, free, no key, cached 1 year | C5 | Automatic |
| NUCC taxonomy CSV | nucc.org (`--nucc`); the auto download failed on 2026-10-18 | C6 (readable names for unmapped taxonomies) | Optional |
| ZIP to county file | e.g. HUD ZIP-COUNTY (`--zip-county`) | C5, C7 regions | Optional (else ZIP3 areas) |

### Outputs

| Folder | What is in it |
|---|---|
| `data/` | Downloaded index files (`raw/`), the C1 table (`index_table.csv`, `rate_files.csv`, `ptd.sqlite`), rate files (`rate_files/`), public files (`public/`), API and geocoder caches |
| `out/c2/` | Plan classification, PPO rate-file list, exclusions, conflicts, validation sample |
| `out/c3/` | Score per file, every metric with its justification, example bad rows |
| `out/c4/` | Network status per reference and per NPI + TIN, `c4_ppo_check.csv`, extraction log |
| `out/rates.sqlite` | The shared rate store C4 to C7 read and write |
| `out/c5/` | Addresses with confidence, every candidate address, the address logic |
| `out/c6/` | Deliverable 1, candidate file, hand-check sample, accuracy report |
| `out/c7/`, `out/c7_candidates/` | Cost scores per rate, provider and TIN; metrics file |
| `out/deliverables/` | Copies of deliverables 1 to 4 and 7b with the names above |

---

## 3. The C0 to C7 flow

The challenge statement splits the work into three core problems (network mapping, grading files, address mapping) and supporting steps. Each step reads the previous step's output.

```mermaid
flowchart TB
  subgraph P1["Core problem 1: Network mapping"]
    S0["Step 0: Scrape Centene page<br/>find the 11 index file links"] --> C1["C1: Parse index files<br/>one table: brand, plan, plan ID,<br/>market, rate file URL"]
    C1 --> C2["C2: Find the PPO network<br/>CA + commercial group + PPO plans<br/>and the rate files they use"]
  end
  subgraph P2["Core problem 2: Grading files"]
    C3["C3: Grade each rate file<br/>9 quality checks, score 0-100"]
  end
  subgraph SUP["Supporting step"]
    C4["C4: Extract rates + decide PPO<br/>one clean rate per NPI + TIN + code<br/>network status per NPI + TIN"]
  end
  subgraph P3["Core problem 3: Address mapping"]
    C5["C5: TIN to NPI to address<br/>score every candidate address"]
  end
  subgraph OUT["Outputs"]
    C6["C6: Provider rate file (Excel)<br/>NPI, TIN, Address, Code, Specialty, Rate"]
    C7["C7: Cost effectiveness<br/>compare with similar providers"]
  end
  C2 --> C3 --> C4 --> C5 --> C6 --> C7
  HN[("Health Net Find a Provider<br/>site (PPO directory)")] -.-> C4
  PUB[("NPPES, NPI Registry API,<br/>CMS Doctors and Clinicians,<br/>Census geocoder")] -.-> C5
  WB[("HiLabs workbook<br/>specialty to billing codes")] -.-> C3
  WB -.-> C4
  WB -.-> C6
  WB -.-> C7
```

| Step | Package | One-line job |
|---|---|---|
| Step 0 + C1 | `ptd_pipeline/` (rishi) | Find the 11 index files and flatten them into one plan table |
| C2 | `ptd_c2/` | Keep California commercial group plans, label each PPO / HMO / EPO / POS / EOA, list the PPO rate files |
| C3 | `ptd_c3/` | Grade each rate file with 9 quality checks |
| C4 | `ptd_c4/` | Pull one clean rate per NPI + TIN + code + variant, and decide each NPI + TIN's network status |
| Directory | `ptd_dir/`, `ptd_c4/dircheck.py` | Search every NPI on Health Net's Find a Provider site |
| C5 | `ptd_c5/` | Find and score each NPI + TIN's practice address |
| C6 | `ptd_c6/` | Write the provider rate file (deliverable 1) and the candidate file |
| C7 | `ptd_c7/` | Score cost against similar providers |
| Runner | `ptd_run/` | Run everything, write the summary, logic file and metrics file |

---

## 4. Each step: flowchart, rules, fixed values and metrics

Every number a judge might want to change lives in `pipeline/config/c2.yaml` to `c7.yaml`, with a comment saying why. The tables below list them ("fixed values") next to the step that uses them. A few defaults are command-line options instead; those are marked "CLI".

### Step 0 and C1: read the index files

**What it does.** Reads the Centene page, finds the index links under "Links to the download TOC files" (skipping the "Centene Employees" section), downloads each index and writes one row per plan x rate file.

```mermaid
flowchart TB
  A["Open centene.com price transparency page"] --> B{"Find heading<br/>'Links to the download TOC files'"}
  B -- "not found" --> X["Stop with an error<br/>(never guess links)"]
  B -- "found" --> C["Take every .json link until the next heading<br/>(Centene Employees section is skipped)"]
  C --> D["Read brand from link text, date from file name<br/>(no URL or date is typed into the code)"]
  D --> E["Download each index file in 1 MB chunks<br/>(retry on errors)"]
  E --> F["Read one reporting_structure at a time<br/>(ijson streaming)"]
  F --> G["Make one row per plan x rate file:<br/>brand, plan name, plan ID, market type, rate file URL"]
  G --> H{"Same 5 values already saved?"}
  H -- "yes" --> I["Drop duplicate"]
  H -- "no" --> J["Save row (SQLite, UNIQUE on all 5 columns)"]
  J --> K["Output: 3,270 plan rows, 33 unique rate files"]
```

| Fixed value | Value | Why |
|---|---|---|
| Section heading looked for | "Links to the download TOC files" | Links are taken by page structure, so the employee "HEALTH NET" link is never picked up |
| Download chunk | 1 MiB, through a `.part` file | Flat memory, no half-written files |
| Dedup key | all 5 columns (`UNIQUE` in SQLite) | `plan_id` is reused and has placeholders like 999999999, so it can't be a key on its own |

**Metrics here:** index files found vs parsed, rows read vs unique rows. **Result:** 11 files, 3,275 pairs, 3,270 unique rows, 33 rate files (checked against an independent `json.load` of every index).

### C2: find the PPO plans and files

**What it does.** Keeps plans that are commercial group, Health Net and California, labels each one PPO / HMO / EPO / POS / HSP / EOA using several kinds of evidence in a fixed priority order, and lists the rate files the PPO plans use.

```mermaid
flowchart TB
  A["3,270 plan rows from C1"] --> B{"Step 1 Scope:<br/>group market? Health Net? California?"}
  B -- "no (1,958)" --> X["Out of scope<br/>logged with the rule that failed"]
  B -- "yes (1,312)" --> C["Step 2 Name signals:<br/>PPO / HMO / EPO / POS / EOA words,<br/>product codes SC, SAL, WholeCare..."]
  C --> D{"Two network types<br/>in one name?"}
  D -- "yes" --> U["UNKNOWN (conflict log)<br/>3 plans"]
  D -- "no" --> E["Step 3 File vote:<br/>do 80% of plans on this file agree?"]
  E --> F["Step 4 Open the rate file:<br/>read network_name on provider references"]
  F --> G{"network_name filled?"}
  G -- "yes" --> H["Use it (strongest evidence)"]
  G -- "no, blank on all 2,825" --> I["File is MIXED:<br/>needs_network_filter = True"]
  H --> J["Step 6 Combine in priority order:<br/>network_name > file vote > plan name > supporting"]
  I --> J
  J --> K["Output: 453 PPO plans, 723 HMO, 127 EOA, 6 POS<br/>all 1,312 share ONE rate file (701.6 MB)"]
  K --> L["Hand-off to C3/C4:<br/>PPO rate file list + allowed-amount files"]
```

| Fixed value (`config/c2.yaml`) | Value | Why |
|---|---|---|
| `scope.keep_market_types` / `keep_brands` / `target_state` | group / healthnet / CA | The brief's scope |
| PPO keywords | `PPO`, `CUSPPO` | Real names include "CA L CUSPPO 15/250/20% RAP" |
| Non-PPO types | HMO, EPO, POS, HSP, EOA | EOA (Elect Open Access) is an HMO-based product; POS is not PPO |
| HMO product codes | SC, SmartCare, SAL, Salud, WholeCare, CommunityCare, Full Network; HSP: PureCare | Health Net product names |
| `EC` (EnhancedCare) | PPO only when no other type is in the name; `include_enhancedcare_ppo: true` | All 140 EC plans sit next to HMO or EOA in the real index |
| `file_vote.label_threshold` | 0.80 | A file is called PPO or non-PPO only when 80% of its other plans agree |
| `hypothesis_gate` | precision 0.90 on at least 10 plans | Weak name hints are tested before they are used |
| `supporting.payment` | ffs share 0.90, capitation share 0.20, at least 20 items | Payment pattern can only adjust confidence, never the label |
| `network_probe.max_bytes` | 200 MB | Enough to read the provider references at the top of a file |
| `validation.sample_size`, `seed` | 25, 42 | Hand-check sample against Summary of Benefits |

**Metrics here:** plans per label, conflicts, decided_by, confidence (high / medium / low). **Result:** 1,312 in scope, 453 PPO, 723 HMO, 127 EOA, 6 POS, 3 conflicts left UNKNOWN. **Key finding:** all 1,312 plans share one rate file with blank `network_name`, so C2 marks it `needs_network_filter = True` and C4 has to split PPO providers from HMO providers inside it.

### C3: grade the rate files

**What it does.** Streams each PPO rate file once and runs 9 checks. Each check gives a mark from 0 to 1; the marks are combined into a score out of 100 and a grade.

```mermaid
flowchart TB
  A["PPO rate file list from C2"] --> B["Stream the file once, item by item<br/>(never loaded whole)"]
  B --> C["Count problems per price row"]
  C --> M1["Schema validity (15)<br/>tolerance 5%"]
  C --> M2["Code coverage (15)<br/>HiLabs workbook codes, volume weighted"]
  C --> M3["Provider coverage (15)<br/>rows with a valid NPI"]
  C --> M4["Zero rates (10)<br/>tolerance 10%"]
  C --> M5["Outliers (10)<br/>log-IQR k=3 AND 3x from median, tol 5%"]
  C --> M6["Rate type mix (10)<br/>share of dollar rates"]
  C --> M7["Duplicates (10)<br/>tolerance 20%"]
  C --> M8["Staleness (10)<br/>expired rows + file age 45 to 365 days"]
  C --> M9["Junk (5)<br/>TIN-only, placeholder TIN, CSTM-ALL"]
  M1 & M2 & M3 & M4 & M5 & M6 & M7 & M8 & M9 --> D["Each check: score = 1 - share / tolerance<br/>(0 at or above tolerance)"]
  D --> E["Weighted mean x 100<br/>(unmeasured checks: weight spread to others)"]
  E --> F{"Grade"}
  F --> G["A 90+ / B 80+ / C 70+ / D 60+ / F<br/>Health Net CA file: 84.3 = B"]
```

**How a check becomes a mark (the "tolerance").** For a "share of bad rows" check, `mark = 1 - share / tolerance`, and anything at or above the tolerance scores 0. Example: with a 10% tolerance for $0 rates, a file with 3% $0 rates scores 0.7.

| Metric (weight) | Tolerance | How it is calculated | This run |
|---|---|---|---|
| `schema_validity` (15) | 5% | Problems per record against CMS schema v2.0.0 (required fields, allowed values, code formats, blank `network_name`) | 0.15% problems, mark 0.97 (2,825 blank network names) |
| `code_coverage` (15) | none | Share of the workbook's 583 codes present, weighted by specialty weight x code share | 134 codes present, weighted 68.1%, mark 0.68 |
| `provider_coverage` (15) | none | Share of price rows tied to a valid NPI (10 digits + Luhn check digit) | 100%, mark 1.00 |
| `zero_rates` (10) | 10% | Rows with rate 0 or less | 0%, mark 1.00 |
| `outliers` (10) | 5% | Outside a log-IQR fence (k = 3) **and** at least 3x from the median of the same code + type + class + setting + modifier | 0.21%, mark 0.96 |
| `rate_type_mix` (10) | none | Share of dollar rates (negotiated, fee schedule, derived) | 100%, mark 1.00 |
| `duplicates` (10) | 20% | Rows repeating an earlier row exactly (code, providers, type, rate, class, setting, service codes, modifiers, expiry) | **97.35%, mark 0.00** (see note) |
| `staleness` (10) | 20% expired; file age 45 to 365 days | Expired rows (9999-12-31 is evergreen) and age of `last_updated_on` | 0% expired, 12 days old, mark 1.00 |
| `junk` (5) | 20% groups, 5% catch-all | TIN-only groups, placeholder TINs, bad NPIs, CSTM-ALL codes | 0%, mark 1.00 |

| Fixed value (`config/c3.yaml`) | Value |
|---|---|
| Weights | 15, 15, 15, 10, 10, 10, 10, 10, 5 (sum 100) |
| Grades | A 90+, B 80+, C 70+, D 60+, else F |
| Outlier fence | `iqr_k: 3.0`, `min_ratio_to_median: 3.0`, groups of 5+ rates |
| Placeholder TINs | 000000000, 999999999, 111111111, 123456789 |
| Duplicate check | file-wide up to 5,000,000 rows, then per item |

**Note on duplicates.** We re-counted this independently: 1,460,328 of the 1,500,288 price rows are revenue-code (RC) rows whose code, providers and price repeat in another item. Health Net publishes revenue code 636 (drugs) once per drug name, 129 times, each with the same 721-entry price table. Whether that counts as "duplicate" depends on whether the drug description is part of a row's identity. Our key leaves the description out, so the check scores 0 and costs the file 10 points. This is stated in the deliverable as a finding about the file.

### C4: extract clean rates

**What it does.** Reads the provider groups (TIN + NPIs) and the prices, keeps the workbook's codes, cleans the price rows and picks one rate per NPI + TIN + code + price variant. The provider references are read by one process while the price items are split over several worker processes.

```mermaid
flowchart TB
  A["Rate file (701.6 MB JSON)"] --> B["Read provider_references<br/>(TIN + its NPIs, 2,825 groups)"]
  A --> C["Read in_network items on N worker processes<br/>(worker k takes every k-th item)"]
  C --> D{"Billing code in the<br/>HiLabs workbook?"}
  D -- "no" --> E["Skip the price, but keep its<br/>network evidence (e.g. capitation)"]
  D -- "yes" --> F["Keep price row with its variant:<br/>rate type, billing class, setting, POS, modifier"]
  F --> G{"Clean?"}
  G -- "capitation / rate 0 or less /<br/>expired / exact repeat" --> H["Drop and count it"]
  G -- "ok" --> I{"Several rates left for the same<br/>NPI + TIN + code + variant?"}
  I -- "no" --> K["Keep it (pick_rule = single)"]
  I -- "yes" --> J["Tie-break in order:<br/>1 best network status<br/>2 more PPO evidence in the contract<br/>3 latest expiry<br/>4 listed by most references<br/>5 lowest rate"]
  J --> K
  K --> L["Rate store (SQLite): one row per<br/>NPI x TIN x code x variant + pick_rule"]
```

| Fixed value | Value | Why |
|---|---|---|
| `codes.scope` | `workbook` (583 codes); `--codes all` keeps every code | The deliverable and C7 weights are about HiLabs' codes |
| `extract.exclude_arrangements` | capitation | A per-member payment is not a per-service price |
| `drop_zero`, `drop_expired`, evergreen date | true, true, 9999-12-31 | $0 and expired rows are not prices |
| `tie_break` | network, ppo_evidence, expiry, support, lowest | Most trustworthy contract first, lowest rate only as a last resort |
| `stream.workers` | CPU cores minus 1 | Parallel parsing; result is the same with 1 or N workers (tested) |
| `stream.batch_rows` | 50,000 | SQLite insert batch |

**Metrics here:** price rows dropped by reason, how often each tie-break rule decided (`pick_rule`). **This run:** 2,720 in-scope price rows, 329 exact repeats merged, 4,826 NPI x TIN x code x variant rates.

### C4: decide which providers are PPO

**Why this is needed.** The file has no network names, so C2's "PPO" only tells C4 which file to read, not which providers in it are PPO. The same doctor can hold a PPO contract and an HMO contract at different prices, under two provider references. So the decision is made **per NPI + TIN pair**, using evidence about that pair.

**Where the evidence comes from: Health Net's own Find a Provider site.**

```mermaid
flowchart TB
  A["Every NPI in the rate file (3,734)"] --> B["Find its city from the NPI Registry"]
  B --> C{"Has a California city?"}
  C -- "no" --> N1["no_ca_location (not searched)"]
  C -- "yes" --> D["Open findaprovider.healthnetcalifornia.com<br/>in a real browser (3 side by side)"]
  D --> E["Set location = city, year = 2026,<br/>network = PPO - Large Group/Small Group"]
  E --> F["Type the NPI in the search box"]
  F --> G["Save the site's own JSON answer<br/>(pces/query: NPI, TIN, address, product, Par/Nonpar)"]
  G --> H{"Listing with product PPO<br/>and Par for 2026?"}
  H -- "yes, same TIN" --> V1["FOUND_PPO_BY_NPI"]
  H -- "yes, other TIN" --> V2["FOUND_PPO_NPI_OTHER_TIN"]
  H -- "no, but same TIN listed" --> V3["NOT_FOUND_BY_NPI_SAME_TIN"]
  H -- "no" --> V4["NOT_FOUND<br/>(never counted as proof of non-PPO)"]
  V1 & V2 & V3 & V4 --> W["Rewrite c4_ppo_check.csv every 50 NPIs<br/>(resumable; reused next run, no new search)"]
```

**How each NPI + TIN gets its status.**

```mermaid
flowchart TB
  A["One NPI + TIN pair<br/>(it can sit in several contracts = provider references)"] --> B{"Does a contract carry its own PPO<br/>network_name, or a PPO-only file?"}
  B -- "yes" --> CP["CONFIRMED_PPO"]
  B -- "no (blank in Health Net CA)" --> C{"Is THIS NPI listed PPO under THIS TIN<br/>on Health Net Find a Provider (2026, Par)?"}
  C -- "yes" --> F{"Another of its contracts<br/>is capitated?"}
  F -- "yes" --> CPD["CONFIRMED_PPO<br/>+ flag dual_contract"]
  F -- "no" --> CP
  C -- "listed only in a non-PPO network" --> NP["CONFIRMED_NON_PPO"]
  C -- "no own listing" --> D{"Best status among its contracts"}
  D -- "contract confirmed only because<br/>OTHER members are listed" --> PC["PPO_CANDIDATE<br/>(downgraded, E1 fix)"]
  D -- "weak evidence: listed under another TIN,<br/>TIN-only match, name + ZIP, shared listing" --> PC
  D -- "every contract capitated<br/>(2% or more capitation rows)" --> NP
  D -- "no evidence at all" --> UR["UNRESOLVED"]
  CP --> D1["Deliverable 1"]
  CPD --> D1
  PC --> D1b["Candidate file 1b"]
  UR --> D1b
  NP --> DX["Rates not extracted"]
```

Rules in one breath: a pair is **confirmed PPO** only by its own contract's network name / a PPO-only file, or by **its own NPI being listed PPO under its own TIN**. Anything inferred from other people (other members of the same contract, the same TIN, a name + ZIP match, a listing under another TIN) can make it a **candidate**, never confirmed. Capitation (an HMO way of paying) makes a contract **non-PPO**, but a pair's own PPO listing wins over it and the pair is flagged `dual_contract`.

| Fixed value | Value | Why |
|---|---|---|
| Site network (CLI `--check-network`) | "PPO - Large Group/Small Group (Platinum, Gold, Silver, Bronze, GF Plans, HSA Compatible, HDHP)", product "HealthNet Commercial PPO", carrier "CA PPO Large and Small" | The employer group PPO network on the site |
| Year (CLI `--check-year`) | this year (2026) | Listing must be `Par` in that year |
| Browsers / save interval / delay (CLI) | 3 / every 50 NPIs / 1 s between searches | Speed vs politeness; resumable |
| Search radius | the site's 50 miles around the NPI's city | Site behaviour, not ours |
| `network.capitation_min_share` | 0.02 | 2% capitated rows marks an HMO-style contract |
| `network.seed_weights` | network_name 1.0, file_label 0.9, directory 0.9, capitation 0.8 | Evidence strength (scores, not probabilities) |
| `directory_weak_factor` | 0.4 | TIN-only or other-TIN matches count 40% and never confirm |
| `decide_min_share` | 0.65 | Share of evidence a side needs to win at contract level |
| `dual_contract` hypothesis | ratio 1.05, 3+ codes, 50% NPI overlap, used only at precision 0.90 on 10+ known pairs | "Higher-paid contract is PPO"; never passed its test on real data, so unused |
| `propagation` hypothesis | 30 iterations, alpha 0.8, 5-fold check, 90% accuracy, 5+ seeds per side, seed 7 | Label spreading over co-priced contracts; unused on real data (only 17-19 non-PPO seeds, no co-priced edges) |

**Metrics here:** status per pair and per contract, the reason (`status_basis`), site verdict per NPI. **This run:** site verdicts 536 found by own NPI, 331 found under another TIN, 241 TIN listed but not the NPI, 1,522 not found; pairs 557 confirmed, 1,768 candidate, 1,440 unresolved, 10 non-PPO; 688 pairs were downgraded to candidate because only other members of their contract were listed; 14 dual contracts.

### C5: link TIN to NPI to address

**What it does.** The rate file is the only public source that says which NPIs bill under which TIN (NPPES hides tax IDs). C5 collects every candidate address for each NPI, cleans them, and scores each one for that NPI + TIN.

```mermaid
flowchart TB
  A["NPI + TIN pair"] --> B["Gather candidate addresses"]
  B --> S1["Health Net directory 0.75"]
  B --> S2["CMS Doctors and Clinicians 0.70"]
  B --> S3["NPI Registry API 0.50"]
  B --> S4["NPPES practice 0.45 / secondary 0.40 / mailing 0.10<br/>(older record = less weight, half-life 3 yrs)"]
  B --> S5["Same TIN listed in directory 0.50"]
  B --> S6["Type 2 NPI of same TIN 0.35"]
  S1 & S2 & S3 & S4 & S5 & S6 --> C["Standardise (USPS style): ST, AVE, STE...<br/>site = house no. + street + ZIP5"]
  C --> D["Census geocoder: two spellings of one place<br/>become one site"]
  D --> E["Score each site (noisy-OR):<br/>p = 1 - product of (1 - best weight per family)"]
  E --> F["TIN consensus: other NPIs of the same TIN<br/>at this site raise p (weight 0.6)"]
  F --> G["Penalties: outside CA x0.3, PO box x0.1,<br/>deactivated NPI gets no address"]
  G --> H["Keep best site + up to 2 more<br/>(each 0.5 or more and 75% of best)"]
  H --> I{"Verified?"}
  I -- "2 independent families agree,<br/>OR directory lists it,<br/>OR confidence 0.8+" --> J["verified = yes"]
  I -- "otherwise" --> K["verified = no (address still kept)"]
```

| Fixed value (`config/c5.yaml`) | Value | Why |
|---|---|---|
| Source weights | directory 0.75, CMS D&C 0.70, NPI API 0.50, same-TIN directory 0.50, NPPES practice 0.45, NPPES secondary 0.40, Type 2 site of same TIN 0.35, mailing 0.10 | How likely each source alone is a current practice site |
| Source families | NPPES variants are one family; only the best of a family counts | They share the same data, so they are not independent witnesses |
| NPPES recency | half-life 3 years, floor 0.4 | Old self-reported records are worth less |
| TIN consensus | weight 0.6, roster of 3+, new-site share 0.5, absent penalty 0.5 | A site most of a TIN shares is a real TIN site |
| Group alignment (CMS `org_pac_id`) | share 0.5, boost 0.5, discount 0.5 | Turns CMS groups into TIN-specific evidence |
| Penalties | outside CA x0.3, PO box x0.1, deactivated NPI = no address | |
| Selection | up to 3 sites; extra site needs 0.5 and 75% of best | Doctors often work at several sites of one group |
| Verified | 2 independent families, or directory listing, or confidence 0.8+ | |
| NPI API | 8 requests/s, 4 threads, 20 failures stop, cache 30 days | Polite use of a free API |
| Geocoder | batches of 1,000, CA only, cache 365 days | Merges two spellings of one place |
| Regions | ZIP3 to California market areas (e.g. 900-908 Los Angeles) | Used by C7 when no ZIP-county file is given |

**Metrics here:** % NPIs and TINs with an address and with a verified address, confidence bands, which sources agree, how often the chosen address differs from NPPES. **This run:** 99.81% of NPIs addressed, 36.13% verified; 739 pairs at confidence 0.8+, 1,521 below 0.4 (NPPES only); 234 best addresses differ from NPPES; 3,490 of 3,732 sites geocoded and 59 merged; 144 pairs at more than one site.

### C6: build the provider rate file

**What it does.** Joins rates and addresses into one row per NPI + TIN + address + code, picks one headline price variant, adds the specialty and splits rows by network status.

```mermaid
flowchart TB
  A["Rates (C4) + addresses (C5)"] --> B["Join on NPI + TIN<br/>one row per NPI + TIN + address + code"]
  B --> C["Pick the headline price variant:<br/>dollar rate, professional for Type 1 / institutional for Type 2,<br/>outpatient, POS 11, no modifier"]
  C --> D["Specialty, first match wins:<br/>1 primary taxonomy via HiLabs crosswalk<br/>2 secondary taxonomy<br/>3 Medicare specialty by name<br/>4 inferred from billed codes<br/>5 NUCC name, else Unmapped"]
  D --> E{"Pair status"}
  E -- "CONFIRMED_PPO" --> F["Deliverable 1: c6_provider_rate_file.xlsx<br/>first 6 columns: NPI, TIN, Address,<br/>Billing Code, Specialty, Rate"]
  E -- "PPO_CANDIDATE / UNRESOLVED" --> G["1b: candidate file (kept, not presented as PPO)"]
  F --> H["1c: hand-check sample, 30 per status<br/>1d: precision with 95% Wilson interval"]
```

| Fixed value (`config/c6.yaml`) | Value |
|---|---|
| Headline variant order | negotiated > fee schedule > derived > per diem > percentage; professional first for Type 1, institutional first for Type 2; outpatient > both > inpatient; POS 11; no modifier |
| Specialty inference | at least 3 workbook codes, F-score 0.25 |
| Deliverable 1 / candidate statuses | CONFIRMED_PPO / PPO_CANDIDATE + UNRESOLVED |
| Hand-check sample | 30 pairs per status |
| Excel limits | 1,000,000 rows per sheet, 3,000,000 rows max (the CSV always has every row) |

**Metrics here:** rows per file, specialty source per NPI, precision of each status from the hand-check sample (Wilson 95% interval; not filled in yet). **This run:** deliverable 1 has 1,155 rows (185 pairs, 117 TINs, 99 codes); every row has a verified address. Specialty source: primary taxonomy 1,368 NPIs, secondary 72, unmapped taxonomy 474, inferred from codes 4.

### C7: cost effectiveness

**What it does.** Compares each rate with what similar providers get for the same code and variant, then combines a provider's codes into one cost index, score and tier. Confirmed PPO and candidate providers are scored separately, so unproven rates never move the PPO benchmark.

```mermaid
flowchart TB
  A["Rates from C6 (confirmed and candidates scored apart)"] --> B["Compare only same code + same variant"]
  B --> C{"Peer group with 5+ providers?"}
  C --> L1["1 specialty + region + NPI type"]
  L1 -- "too few" --> L2["2 specialty + NPI type (statewide)"]
  L2 -- "too few" --> L3["3 NPI type (statewide)"]
  L3 -- "too few" --> L4["4 all providers"]
  L1 & L2 & L3 & L4 --> D["Rate index = rate / peer median"]
  D --> E["Provider cost index = weighted geometric mean<br/>weights = HiLabs SPEC_PROC_WEIGHT"]
  E --> F["Shrink toward 1.0: x n_eff / (n_eff + 2)<br/>(few codes = weak evidence)"]
  F --> G["Score 0-100 = percentile among similar providers<br/>(100 = cheapest)"]
  F --> H["Tier: <=0.85 Highly cost-effective, <=0.95 Cost-effective,<br/><=1.05 At market, <=1.20 Above market, else High cost"]
  F --> I["Confidence: high n_eff 8+ and 30% coverage,<br/>medium n_eff 3+, else low"]
```

| Metric | Formula | Why |
|---|---|---|
| Rate index | rate / peer median | 1.00 = paid like similar providers |
| Cost index | weighted geometric mean of rate indexes, weights = HiLabs `SPEC_PROC_WEIGHT` | Ratios average on a log scale; common codes count more |
| n_eff | (sum w)^2 / sum w^2 | Effective number of codes behind the index |
| Shrunk index | exp(ln(index) x n_eff / (n_eff + 2)) | One cheap code is weak evidence |
| Cost score 0-100 | 100 x (1 - percentile among similar providers) | 100 = cheapest |
| TIN index | n_eff-weighted mean of members' log indexes | Group-level view for contracting |

| Fixed value (`config/c7.yaml`) | Value |
|---|---|
| Minimum peers | 5 |
| Peer levels | specialty + region + type, specialty + type, type, all |
| Shrinkage k | 2.0 |
| Cohort for the score | specialty + region, then specialty, then all; 10+ providers |
| Tiers | 0.85 / 0.95 / 1.05 / 1.20 |
| Confidence | high: n_eff 8+ and 30% coverage; medium: n_eff 3+ |
| Left out | percentage-of-charges rates |

**This run:** 690 confirmed rates benchmarked, 135 providers and 81 TINs scored; tiers 13 / 7 / 98 / 11 / 6 (most "At market" because each provider has few priced codes).

---

## 5. How to run it

### Browser UI

From the repository root, install the pipeline dependencies and start the UI:

```bash
python -m pip install -r pipeline/requirements.txt
python run_ui.py
```

Open the localhost URL printed by the server (normally `http://127.0.0.1:8000`).
The UI reads the real `pipeline/out/rates.sqlite` database, C2/C3/C5/C6/C7 reports
and C7 benchmark CSVs. There is no sample-data fallback. Before the first pipeline
run, the dashboard shows missing results and exports remain disabled.

The analyst views: **Rate explorer** (filter by NPI, TIN, provider name, billing
code, specialty, region, network status, cost tier or rate range, sort by rate),
**Compare providers** (up to six NPI + TIN providers side by side, code by code and
price variant by price variant, against the market median, with a basket total over
the codes they all bill), **Price spread by code** (quartiles, distribution and a
ranked list of every provider's rate for one code and variant), **TIN → NPI →
address** (how many NPIs each TIN has, how many sites each pair keeps, and one TIN's
tree of NPIs and ranked practice sites with confidence and sources), plus the C7
benchmarks, C2 plan decisions and C3 file quality.

The HiLabs workbook and the saved PPO directory check are not typed into the UI:
the runner reads them from `pipeline/inputs/` (see "Inputs kept in the repo"
below), and **Run pipeline** shows which ones it found. Use **Run pipeline** to
choose the start/end stages and optional public input files. Paths refer to files on the machine running the
server and can be absolute or relative to `pipeline/`. The server executes
`python -u -m ptd_run all` from `pipeline/`, streams its actual logs and reports
its exit code. Only one run can execute at a time. Results refresh after it ends;
full run logs are saved in `pipeline/out/ui_runs/`. Closing the UI server stops
its active run. Closing only the browser tab leaves the server and run active.

The confirmed-PPO CSV/Excel downloads are C6's actual deliverables; candidate
rates have a separate, explicitly labelled download. Cost scores use the
confirmed and candidate C7 tables independently. Per-code peer medians and
quartiles come from C7's generated benchmark files.

Optional launch flags: `python run_ui.py --port 8001 --no-browser` and
`--pipeline-dir /path/to/pipeline` (a directory containing both the pipeline code
and its `data/` and `out/` folders). No npm installation is needed.

### Once

```bash
git clone https://github.com/Medix1075/HiLabs_Hackathon.git
cd HiLabs_Hackathon/pipeline
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium   # only needed for the Health Net site check
```

### Inputs kept in the repo

Put these two files in `pipeline/inputs/` and every run picks them up without flags:

| File | If it is missing |
|---|---|
| `pipeline/inputs/Rate Benchmarking Individual Weights_Hackathon.xlsx` (HiLabs workbook) | every billing code is extracted and C7 weights codes equally |
| `pipeline/inputs/c4_ppo_check.csv` (saved Health Net PPO directory check) | C4 searches the Health Net directory live (Playwright; resumable; 12 to 13 hours on the whole file) |

With the saved check present, C4 uses it as is and does not search the site. A path on the command line (`--workbook`, `--ppo-check-csv`) always wins; `--no-check-directory` or `PTD_DIRECTORY_CHECK=off` stop the live search. Details: `pipeline/inputs/README.md`.

### The whole pipeline, reusing a saved PPO check (about 6 minutes)

This is the command used for the results above. It downloads the public files, runs Step 0 to C7 and reuses the saved Health Net site results, so no site search is needed:

```bash
python -m ptd_run all --fetch-public \
  --npis inputs/c4_ppo_check.csv \
  --as-of 2026-10-18
```

- The workbook and the saved site check come from `pipeline/inputs/`.
- `--npis` limits C4 to C7 to the NPIs that were searched. Leave it out to cover every NPI in the file; unsearched NPIs can then only be candidates or unresolved.
- `--as-of` fixes the date used for expiry and record age, so two runs give the same rows.

### The whole pipeline from scratch, including the site search (about 12 to 13 hours)

```bash
python -m ptd_run all --fetch-public --check-year 2026 --as-of 2026-10-18
```

With no `pipeline/inputs/c4_ppo_check.csv`, the runner turns the live search on by itself (`--check-directory` does the same explicitly).

The search runs 3 browsers, rewrites `out/c4/c4_ppo_check.csv` every 50 NPIs and can be stopped and restarted: it skips NPIs already searched. Run it on a machine that stays awake (a cloud container that pauses kills the browsers).

### Parts of the pipeline

```bash
python -m ptd_run all --from c4 --to c7 ...      # rerun later steps on earlier outputs
python -m ptd_run all --from c5 --nppes data/public/<NPPES zip> --dac data/public/<DAC csv> --workbook ...   # skip re-downloading
python -m ptd_pipeline                           # Step 0 + C1 only
python -m ptd_c2 run                             # C2
python -m ptd_c3 run --expected-codes "<workbook>"   # C3
python -m ptd_c4 run --workbook "<workbook>" --ppo-check-csv c4_ppo_check.csv
python -m ptd_c5 run --nppes ... --dac ...
python -m ptd_c6 run --workbook "<workbook>"
python -m ptd_c7 run --workbook "<workbook>"                       # confirmed set
python -m ptd_c7 run --workbook "<workbook>" --set candidates --out out/c7_candidates
python -m ptd_run summary                        # summary, logic file, metrics file, deliverables
python -m ptd_c6 check-network --sample out/c6/c6_network_check_sample.csv   # after filling it in by hand
```

Offline switches: `PTD_NPI_API=off`, `PTD_GEOCODE=off`. Tests: `PTD_NPI_API=off pytest` (including UI integration tests against the real C3–C7 runner, all offline).

Known quirk: C1 writes into `pipeline/data/` even when `--data` points elsewhere, so run from inside `pipeline/` with the default folders.

---

## 6. How long each step takes, and the bottlenecks

Measured on the run above (cloud container, 4 CPU cores, 2026-10-18), from each stage's start and finish times in its `*_run_summary.json` and log. Total: **5 minutes 34 seconds**, excluding the Health Net site search (reused from the CSV).

| Step | Time | What dominates |
|---|---|---|
| Download NPPES (1.16 GB) + CMS D&C (0.9 GB) | 58 s | Network; skip it on reruns by passing `--nppes` / `--dac` |
| Step 0 + C1 | 2 s | 11 small index files |
| C2 | 4.6 s | Reading the top of the rate file for network names |
| C3 | 29.5 s | Downloading and streaming 701.6 MB / 1.5M price rows once |
| C4 | 14.6 s | Reading the file with 3 workers (12.4 s); tagging 0.2 s; extraction 0.1 s |
| C5 | 221.5 s | Scanning all 9.85M NPPES rows (136 s); Census geocoder for 3,732 sites (about 70 s); the rest about 15 s |
| C6 | 1.2 s | Writing the Excel |
| C7 | 2.5 s | Both sets |

**Time to map NPIs to PPO status and an address with confidence.**

| Part | Time per NPI | 3,734 NPIs (whole file) | How we know |
|---|---|---|---|
| Health Net site search (3 browsers) | about 12 s (about 36 s per browser) | **about 12.4 hours** | Measured: the saved CSV grew from 1,503 NPIs (08:29 UTC) to 2,603 NPIs (12:08 UTC), about 5 NPIs a minute. The full-file time is a projection |
| C4 network status from the saved CSV | under 0.001 s | under 1 s | Measured (0.2 s for 3,775 pairs) |
| C5 address with confidence | about 0.085 s | about 5 minutes | Measured 221.5 s for 2,596 NPIs; most of it is a fixed NPPES scan, so it barely grows with more NPIs |
| NPI Registry API (only when no NPPES file is given) | 0.125 s (8 per second) | about 8 minutes | From the configured rate limit; an earlier run reported about 6 minutes for 2,800 NPIs. Estimate |

**Bottlenecks, biggest first.**

1. **The site search is 99% of the end-to-end time** (about 12 hours vs 6 minutes). It drives a real browser per NPI because the site is a JavaScript app with no bulk download. Ways to cut it: call the site's own search API (`pces/query`) directly instead of clicking through pages (the responses are already captured, so the request shape is known), run more browsers, search by TIN or group name once per TIN instead of once per NPI, and never search an NPI twice (already done: the CSV is reused).
2. **NPPES scan (136 s).** C5 reads all 9.85M national rows to find about 2,600 NPIs. `python -m ptd_c5 extract-nppes --state CA` makes a California-only file once; the 40-NPI test scanned the 1.26M-row CA extract in 9 s.
3. **Geocoder (about 70 s).** One request per 1,000 sites; answers are cached for a year, so a rerun is nearly free.
4. **C3 reads the rate file a second time (29.5 s).** C3 and C4 each stream the 701.6 MB file. A combined pass would save about 15 s; not worth the extra coupling at this size.
5. **Downloads (58 s).** Only on the first run.

For scale: on a 385 MB synthetic file with 40,000 references and 15.9M output rates, C4 took 73 s and 434 MB of memory. The design streams item by item, shards work across processes, and stores repeated provider lists once ("refsets"), so memory stays flat as files grow.

---

## 7. Known limits

- **Deliverable 1 depends on the site search.** Only NPIs the Health Net site lists under their own NPI and TIN are confirmed. Organisations are listed far less often than individual doctors, so many real PPO groups stay candidates. The finished search, a TIN or name search, and the planned PPO probability model would narrow this.
- **Precision of "confirmed" is not measured yet.** Fill in `1c_network_check_sample.csv` and run `ptd_c6 check-network`.
- **The rate file covers only 134 of the 583 workbook codes**, so cost scores rest on 2 or 3 codes per provider.
- **About two thirds of addresses rest on NPPES alone** (confidence below 0.4). CMS Doctors and Clinicians covers only Medicare-enrolled individuals.
- **The duplicate check scores 0** because of the repeated revenue-code tables (see C3).
- **Weights and tolerances are judgement, not fitted.** The workbook weights come from HiLabs; everything else is a documented default in `config/`.
- **Live sources drift.** The site, the NPI API and the geocoder are live. Reuse the saved CSV and pass `--as-of` for repeatable runs.

---

## 8. Repository layout

```
pipeline/
├── config/c2.yaml … c7.yaml   every threshold, weight and rule, with the reason
├── ptd_pipeline/              Step 0 + C1 (scraper.py, index_parser.py)
├── ptd_c2/                    C2: scope, name_signals, file_vote, network_probe, supporting, combine, validation
├── ptd_c3/                    C3: scan (streaming pass), metrics, codes (workbook weights), tags
├── ptd_c4/                    C4: ingest (parallel streaming), network (statuses), extract (tie-break),
│                              dircheck (site search), ppo_check (CSV), directory, oon
├── ptd_dir/                   Health Net site driver, capture/replay/build for directory data
├── ptd_c5/                    C5: address standardiser, sources, resolve (scoring), geocode, fetch, logic
├── ptd_c6/                    C6: rate file, specialty, network_check (precision)
├── ptd_c7/                    C7: bench (peer groups), score, report
├── ptd_run/                   one-command runner, summary, logic file, metrics file
├── tools/                     synthetic rate file generator, network structure probe
├── tests/                     113 offline tests, including a synthetic California world with known answers
└── README.md                  detailed technical reference for every stage
```

Context: Health Net is ending California commercial group plans, with most policies ending 2027-02-28.
