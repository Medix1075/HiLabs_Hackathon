# C2: Centene California commercial group PPO plans and rate files

This stage takes your **C1 table** (`brand`, `plan name`, `plan ID`, `market type`, `rate file URL`) and works out which plans and rate files make up Health Net's **California commercial group PPO network**. Every row gets a final network type, a confidence level and a one-line reason. The output includes the list of PPO rate files that C3 (quality) and C4 (rates) read next.

It implements the seven steps of the C2 brief (scope filters, plan-name signals, rate-file association, `network_name` confirmation, supporting evidence, combined scoring, spot-check validation).

## Run it

```bash
cd pipeline
python3 -m pip install -r requirements.txt

# Full run: C1 table + the raw index JSONs (local files, a folder, or URLs)
python3 -m ptd_c2 run --c1 path/to/c1_output.csv --index path/to/raw_indexes/ --out out/c2

# Offline run (no rate/OON file reads; network_name not confirmed, confidence capped at medium)
python3 -m ptd_c2 run --c1 path/to/c1_output.xlsx --out out/c2 --no-probe

# After filling in the validation sample by hand (Step 7)
python3 -m ptd_c2 validate --sample out/c2/c2_validation_sample.csv --out out/c2

# Tests (spin up a local HTTP server with Centene-shaped fixture files)
python3 -m pytest -q tests
```

**C1 input:** CSV, TSV, XLSX (`--sheet` to pick a sheet), JSON or JSONL. Column names are matched loosely, so `plan name`, `plan_name` and `PlanName` all work. Exact duplicates are dropped again and counted.

**`--index` is optional** but needed for the out-of-network signal, because C1 did not keep `allowed_amount_file`. Pass the same index files your C1 read. Index and rate files may be plain JSON or gzip, local or remote.

**Network use:** Step 4 streams the start of each in-scope rate file and stops as soon as it has the `network_name` values plus a 200-record sample. Results are cached in `out/c2/network_probe_cache.json`, so a rerun only retries files that failed.

## What each step does

| Step | Module | What it decides |
|---|---|---|
| 1 Scope | `scope.py` | Keeps `group` market, Health Net, state CA. State comes from the plan name prefix (`CA L ...`), a HIOS plan id (`67138CA060`), the file name (`..._ambetter-ok_in-network`), or the brand's home state as a flagged fallback. The word "PPO" plays no part: Ambetter `Gold PPO` falls to the market rule and QualChoice `PPO Complete` to the brand and state rules. |
| 2 Name signals | `name_signals.py` | Tags `PPO/HMO/EPO/POS/HSP` keywords and product codes (`SC`, `SAL`, `WholeCare`, `CommunityCare`, `Full Network`, `PureCare`). `EC` counts as EnhancedCare PPO only when no HMO marker is present. Names with two types become `conflict`. The **hypothesis signals** (`L&T`/`Life` hint, coinsurance-tail vs `d/` deductible shape) are first tested on plans with explicit keywords; each is used only if it reaches ≥90% precision on ≥10 plans. Otherwise it is logged and not used. |
| 3 File vote | `file_vote.py` | Groups rows by rate file URL and counts PPO vs non-PPO names. A file is `PPO` or `NON_PPO` at ≥80% share (configurable), otherwise `MIXED` or `NO_VOTES`. A plan that is alone in its file doesn't get to count its own name as an association vote. `plan_id` is never used as a key. |
| 4 network_name | `network_probe.py` | Single streaming `ijson` pass over the rate file (gzip handled): `provider_references[].network_name` (string or list), the TINs/NPIs per group, and `location` references, which it fetches when no names are inline. Names are mapped to `PPO_FULL`, `PPO_ENHANCEDCARE`, `HMO`, `HSP`, `EPO`, `POS` by ordered rules in config. A file with both PPO and non-PPO names is `MIXED`: its PPO plans stay in scope with `needs_network_filter = True`, and the PPO network names are listed for C4 to filter on. |
| 5 Supporting | `supporting.py` | `oon_signal`: whether the plan's `allowed_amount_file` has `out_of_network` records. `payment_signal`: ffs vs capitation share and distinct TINs in the sampled `in_network` items. Both are **tested against network_name-confirmed rows in the same run**. Each one adjusts confidence only if it agrees ≥90% of the time; it never changes a label. |
| 6 Combine | `combine.py` | Priority is network_name > file vote > plan name > supporting. Confidence: **high** needs network_name with no disagreement from the plan's own name; **medium** for a file vote backed by other plans, or network_name that overrode the name; **low** for a name-only decision or a mixed file. Every disagreement is written to the conflicts log. |
| 7 Validation | `validation.py` | Writes a seeded random sample with blank `source_label` / `source_url` columns. You fill them from Health Net Summary of Benefits PDFs or the provider directory, then `validate` computes match and agreement rate. It never scrapes. |

## Outputs (`out/c2/`)

| File | Contents |
|---|---|
| `c2_classification.csv` | One row per C1 row (plan × rate file), out-of-scope rows included: `network_names`, `name_signal`, `file_vote`, `oon_signal`, `payment_signal`, `final_network_type`, `ppo_variant`, `in_scope_ppo`, `needs_network_filter`, `confidence`, `decided_by`, `reason` |
| `c2_ppo_rate_files.csv` | **Hand-off to C3/C4:** distinct in-scope PPO rate file URLs with variant, PPO network names to filter on, and confidence |
| `c2_exclusions.csv` | Every row Step 1 removed, the primary rule, all rules failed, and whether the name said "PPO" |
| `c2_name_signals.csv`, `c2_file_votes.csv`, `c2_network_probe.csv`, `c2_supporting.csv` | Per-step evidence, including vote counts, bytes read, stop reason and errors |
| `c2_conflicts.csv` | Every conflict and how it was resolved: `name_conflict`, `name_vs_vote`, `name_vs_network_name`, `vote_vs_network_name`, `mixed_file`, `supporting_vs_final`, `unresolved` |
| `c2_hypotheses.csv` | Measured result for each hypothesis: support, precision, breakdown, and whether it was used |
| `c2_validation_sample.csv` | Step 7 sheet to fill in |
| `c2_results.xlsx` | The main tables as sheets in one workbook |
| `c2_run_summary.json`, `c2_run_log.txt` | Counts, config used, context notes |

## Decisions you can flip in `config/c2.yaml`

- **EnhancedCare PPO is in scope by default** (`include_enhancedcare_ppo: true`). It is a real PPO in the California commercial group book, sold by Health Net Life, with out-of-network benefits, but its network is narrow (preferred benefits in LA County only). Every row carries `ppo_variant` (`FULL` / `ENHANCEDCARE`), so C3/C4 can split it out even with the flag on.
- **POS is not PPO.** It counts against PPO in the file vote.
- Plans with no state in the name are treated as CA when the brand is Health Net, and flagged `brand_default (inferred)`. Set `require_explicit_state: true` to drop them.
- Thresholds (vote share, hypothesis gate, probe byte cap, sample sizes) and every keyword and network-name rule live in the config. The code has no URLs or dates.
- `employer_tiebreaker` (same-EIN plans) is off by default because it is a very weak signal.

## What was and wasn't run against real data

- **Not run on real Centene files.** This cloud sandbox cannot reach centene.com (the proxy refuses the connection), so the code has never seen a real Health Net rate file. Run it on your machine first and check `c2_network_probe.csv`.
- **Tested on fixtures**: 37 tests serve Centene-shaped files over a local HTTP server. The plan names are the real ones from the research notes, and the rate files follow schema 2.0.0. The tests cover gzip, `network_name` as a string and as a list, `in_network` before `provider_references`, `location` references, a mixed file, an unreachable file, a file whose network contradicts its plan names, the scope traps, and the EnhancedCare flag both ways.
- **Scale check** (synthetic gzip files): a file with 100k provider references read only 16 MB in 1.4 s at 44 MB RAM before it stopped. A file that puts `in_network` *before* `provider_references` hit the 200 MB cap without reaching any `network_name` and was reported `UNCONFIRMED` (the code never guesses). If real Health Net files are laid out like that, raise `network_probe.max_bytes`, or let those rows fall back to the file vote.

**Things to check on the first real run:**
1. The actual `network_name` strings Health Net uses. If any land as `UNKNOWN` in `c2_network_probe.csv`, add a rule to `label_rules`.
2. Whether `provider_references` comes first in the file. Look at `stop_reason` and `bytes_read`.
3. `c2_hypotheses.csv`, to see which name and supporting hypotheses held up on real data.

Context: Health Net is ending California commercial group plans, with most policies ending 2027-02-28. This is logged in every run.
