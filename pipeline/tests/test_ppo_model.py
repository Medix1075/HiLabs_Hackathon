"""C6's optional PPO probability model (ptd_c6/ppo_model.py) and C7's probability-weighted peer statistics."""
import numpy as np

from ptd_c6.ppo_model import auc, final_probability, fit_logistic
from ptd_c7.bench import group_stats, weighted_group_stats


def test_logistic_fit_recovers_the_direction_of_a_signal():
    rng = np.random.default_rng(0)
    x = rng.normal(size=2000)
    y = (rng.random(2000) < 1 / (1 + np.exp(-(2 * x - 0.5)))).astype(float)
    X = np.column_stack([np.ones_like(x), x])
    beta = fit_logistic(X, y, np.ones_like(x), l2=1.0)
    assert 1.6 < beta[1] < 2.4 and -0.8 < beta[0] < -0.2
    assert auc(y, X @ beta) > 0.8


def test_auc_counts_ties_half_and_needs_both_classes():
    assert auc(np.array([1, 0]), np.array([0.5, 0.5])) == 0.5
    assert auc(np.array([1, 1, 0, 0]), np.array([0.9, 0.8, 0.2, 0.1])) == 1.0
    assert auc(np.array([1, 1]), np.array([0.3, 0.4])) is None


def test_observed_evidence_overrides_the_model():
    assert final_probability("CONFIRMED_PPO", 0.1) == 1.0
    assert final_probability("CONFIRMED_NON_PPO", 0.9) == 0.0
    assert final_probability("PPO_CANDIDATE", 0.123456) == 0.1235


def test_equal_weights_give_the_unweighted_benchmark():
    rng = np.random.default_rng(1)
    gid = rng.integers(0, 4, 200)
    v = rng.integers(50, 150, 200).astype(float)  # with ties
    a = group_stats(gid, v, 4)
    b = weighted_group_stats(gid, v, np.full(200, 0.3), 4)
    for x, y in zip(a, b):
        assert np.allclose(x, y)


def test_unlikely_ppo_rates_pull_the_median_less():
    gid = np.zeros(5, dtype=np.int64)
    v = np.array([10.0, 20, 30, 40, 50])
    med = weighted_group_stats(gid, v, np.array([1, 1, 1, 0.05, 0.05]), 1)[2][0]
    assert med < 30
