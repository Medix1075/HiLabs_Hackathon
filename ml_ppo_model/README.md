# PPO probability model: data, weights and results

Everything the optional `--ppo-model` step (code in `pipeline/ptd_c6/ppo_model.py`) and its check against real labels produced, from the 2026-10-18 run. The method and the results are explained in section 5 of the [main README](../README.md#5-optional-the-ppo-probability-model).

| Folder | Files |
|---|---|
| `datasets/` | `outside_sample_dataset.csv` (914 outside providers with real PPO / HMO labels, weighted by `w`), `rate_file_npis_labelled.csv` (3,649 rate-file NPIs with real labels and both models' scores) |
| `weights/` | `ppo_model_coefficients.csv` (34 standardised coefficients), `model_report.md` (training counts, c = 0.5826, reliability) |
| `results/` | `holdout_out_of_time.json` (AUC 0.876 vs 0.49), `factor_check_results.json` (factor shares and AUC comparison), `r_neighbours.png` |
| `scripts/` | `probe.py`, `collect.py`, `q.py`, `build.py`, `analyze.py` to rebuild the datasets |

`scripts/reqs.json` (the finder's saved search request, which holds the site's public key) and `scripts/work/` are git-ignored.
