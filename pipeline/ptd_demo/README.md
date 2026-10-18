# Temporary demo path: reviewed Health Net PPO roster -> rates -> address mapping

This is an **opt-in demo** on branch `demo/ppo-c4-c6`. It does not replace or modify the production C1-C7 path.

## What the demo does

1. Reads `data/ppo_check_results.csv` (40 reviewed rows in the supplied demo; unique NPI+TIN pairs).
2. C4 reads the actual Health Net California transparency rate file for price data. The demo CSV is used only as a temporary PPO allowlist:
   - `FOUND_PPO_BY_NPI`, `FOUND_PPO_SAME_FACILITY_DIFFERENT_NPI`, and `NOT_FOUND_BY_NPI_SAME_TIN_OTHER_SITES_PPO` are treated as PPO matches for demo exploration.
   - `NOT_FOUND` is **unresolved**, not non-PPO; no rates for those rows are presented as confirmed PPO.
   - Only an exact NPI+TIN pair match is labelled, not every provider in the same reference.
3. C5 resolves addresses for the CSV's NPI+TIN pairs, even if they do not have a surviving price row. Provide NPPES and/or CMS Doctors & Clinicians data for address results. Without any source, addresses remain blank rather than being guessed.
4. C6 reads the C4 extracted rates plus C5 address/specialty tables and writes the standard rate output and candidate output.

**Important:** `rate_file_ref` in `ppo_check_results.csv` is a provider-reference identifier, not the JSON rate-file URL. The CSV has no negotiated rate or billing code data, so it cannot stand alone as deliverable 1. The confirmed rows map to 19 PPO-verdict rows in this snapshot; 21 `NOT_FOUND` rows remain unresolved. After exact NPI+TIN matching, the runner reports which of those rows actually exist in the source PTD and have usable extracted prices.

## Run

From the repository's `pipeline/` directory:

```bash
mkdir -p data
cp /path/to/ppo_check_results.csv data/ppo_check_results.csv
# Optional but recommended: place the HiLabs workbook at the path below.
python -m ptd_demo.c4 --demo-csv data/ppo_check_results.csv \
  --workbook "Rate Benchmarking Individual Weights_Hackathon.xlsx"
python -m ptd_demo.c5 --demo-csv data/ppo_check_results.csv \
  --nppes data/public/NPPES_Data_Dissemination.zip \
  --dac data/public/DAC_NationalDownloadableFile.csv
python -m ptd_demo.c6 --demo-csv data/ppo_check_results.csv \
  --workbook "Rate Benchmarking Individual Weights_Hackathon.xlsx"
```

If you need a small smoke test, C4 accepts `--max-items 100` (the result is deliberately partial and should not be used as the final rate file). If running without the workbook, pass `--codes all` to C4, but C6 still needs the workbook for the required specialty/coding crosswalk.

The demo database is isolated at `out/demo/rates.sqlite`; outputs go under `out/demo/c4`, `out/demo/c5`, and `out/demo/c6`. Do not move these outputs into deliverables without clearly labelling that the network labels are based on the small demo roster.
