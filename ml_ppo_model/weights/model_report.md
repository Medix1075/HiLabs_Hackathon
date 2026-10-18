# PPO probability model

Positive-unlabelled logistic regression (see ptd_c6/ppo_model.py). g = P(listed on the finder | features), P(PPO) = g / c with c = P(listed | PPO) estimated on held-out listed pairs.

- Searched pairs used for training: 2,780 (945 listed, 594 under the same TIN)
- Held-out AUC, listed vs not (5 folds grouped by TIN): 0.8660213658578781
- c = P(listed | PPO): 0.5826

## Held-out reliability (g vs observed listing share)

| g from | g to | pairs | mean g | listed share |
|---|---|---|---|---|
| 0.002 | 0.026 | 557 | 0.015 | 0.022 |
| 0.026 | 0.102 | 563 | 0.053 | 0.048 |
| 0.102 | 0.369 | 557 | 0.24 | 0.305 |
| 0.369 | 0.596 | 557 | 0.476 | 0.573 |
| 0.596 | 0.982 | 556 | 0.754 | 0.754 |

## Coefficients (standardised features)

| feature | coefficient |
|---|---|
| npi_type_2 | -1.3361 |
| family=behavioural | -1.3111 |
| ref_searched | -0.66 |
| ref_found_share | 0.5559 |
| family=medical | -0.4667 |
| family=surgical | -0.4383 |
| n_refs | 0.4117 |
| tin_found_share | 0.3639 |
| family=facility | 0.3392 |
| tin_searched | -0.2737 |
| code_fit | -0.239 |
| region=San Gabriel / San Fernando Valleys | 0.238 |
| region=Los Angeles | 0.2338 |
| tins_per_npi | -0.2208 |
| region=Orange County | 0.215 |
| region=Fresno / Central Valley | 0.2115 |
| region=San Jose / South Bay | 0.2085 |
| family=primary_care | -0.1665 |
| region=East Bay | 0.1578 |
| ref_size | -0.1463 |
| region=San Diego | 0.1297 |
| region=Inland Empire | 0.1257 |
| capitated | -0.1013 |
| family=hospital_based | -0.1012 |
| region=Bakersfield / Kern | 0.0888 |
| region=San Francisco | 0.0784 |
| region=Northern California | 0.0761 |
| region=Stockton / Modesto | 0.0734 |
| region=Ventura / Santa Barbara | 0.0727 |
| region=Peninsula | 0.0269 |
| region=Sacramento | 0.0269 |
| region=North Bay | 0.0243 |
| region=High Desert | 0.0141 |
| family=therapy | 0.0059 |

## PPO probability by network status (every NPI + TIN pair)

| status | pairs | mean P | P >= 0.5 | P >= 0.8 |
|---|---|---|---|---|
| CONFIRMED_PPO | 594 | 1.0 | 594 | 594 |
| PPO_CANDIDATE | 1358 | 0.413 | 529 | 314 |
| UNRESOLVED | 922 | 0.304 | 263 | 93 |
