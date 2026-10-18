# Pipeline inputs kept in the repo

`python -m ptd_run all` (and the UI's **Run pipeline**) read these two files from this folder, so nobody
has to pass them on the command line or type them into the UI.

| File | What it is | If it is missing |
|---|---|---|
| `Rate Benchmarking Individual Weights_Hackathon.xlsx` | HiLabs specialty -> billing code workbook (sheets *Taxonomy-Spec Xwalk*, *Specialty Weights*, *Spec-Proc Weights*). Any other single `.xlsx` here is used too. | Every billing code is extracted and C7 weights codes equally. |
| `c4_ppo_check.csv` | Saved Health Net PPO directory check, as written by C4 (`out/c4/c4_ppo_check.csv`; columns `npi, tin, ..., site_checked, ..., verdict`). Excel's tab-separated save also works. The early 40-NPI `ppo_check_results.csv` layout is read too, but its listings carry no TIN, so prefer the C4 table. Any other CSV here is ignored, and C4 stops with an error if given one directly. | C4 searches the Health Net directory live (needs Playwright + Chromium; resumable; hours on the whole file). |

A path given on the command line (`--workbook`, `--ppo-check-csv`) always wins.
`--no-check-directory` or `PTD_DIRECTORY_CHECK=off` stop the live search when no saved check is here.
