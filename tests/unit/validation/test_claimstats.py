"""Claim statistics for the M04 protocol (#132; PRD #120 rules 2-5). Each estimator is
checked against a closed form or a textbook value, or by simulation with a fixed seed."""

import math
import random
import statistics
from collections.abc import Callable

import pytest

from app.validation.claimstats import (
    Comparison,
    aa_noise_floor,
    best_of_n,
    bootstrap_ci,
    brier_skill_ci,
    compare_to_floor,
    deflated_sharpe,
    ece_binned,
    ece_debiased,
    ece_noise_floor,
    ece_noise_floor_consistency,
    ece_sweep,
    ece_sweep_binary,
    expected_binned_ece,
    expected_max_sharpe,
    holm,
    mean_ci,
    mean_lb_power,
    min_track_record_length,
    pbo_cscv,
    probabilistic_sharpe,
    sign_test,
    simulated_power,
)

# --- block bootstrap -------------------------------------------------------------------


def test_bootstrap_is_reproducible_with_a_seed() -> None:
    rng = random.Random(1)
    xs = [rng.gauss(0, 1) for _ in range(50)]
    a = mean_ci(xs, block=5, resamples=500, seed=7)
    b = mean_ci(xs, block=5, resamples=500, seed=7)
    assert a == b


def test_a_constant_series_has_a_degenerate_interval() -> None:
    ci = mean_ci([2.0] * 30, block=5, resamples=200, seed=1)
    assert (ci.point, ci.low, ci.high) == (2.0, 2.0, 2.0)


@pytest.mark.parametrize("scheme", ["stationary", "circular"])
def test_iid_interval_matches_the_normal_width(scheme: str) -> None:
    rng = random.Random(3)
    xs = [rng.gauss(0, 1) for _ in range(400)]
    ci = mean_ci(xs, block=1, resamples=2000, seed=4, scheme=scheme)  # type: ignore[arg-type]
    se = statistics.stdev(xs) / math.sqrt(len(xs))
    assert ci.high - ci.low == pytest.approx(2 * 1.96 * se, rel=0.12)
    assert ci.low < ci.point < ci.high


def test_blocks_widen_the_interval_for_autocorrelated_days() -> None:
    rng = random.Random(5)
    xs, x = [], 0.0
    for _ in range(600):
        x = 0.8 * x + rng.gauss(0, 1)  # AR(1): neighbouring days are not independent
        xs.append(x)
    iid = mean_ci(xs, block=1, resamples=1000, seed=6)
    blocked = mean_ci(xs, block=20, resamples=1000, seed=6)
    assert (blocked.high - blocked.low) > 1.8 * (iid.high - iid.low)


def test_a_one_sided_bound_has_no_upper_limit() -> None:
    rng = random.Random(8)
    xs = [rng.gauss(1, 1) for _ in range(100)]
    two = mean_ci(xs, block=5, resamples=1000, seed=9, level=0.90)
    one = mean_ci(xs, block=5, resamples=1000, seed=9, level=0.95, one_sided=True)
    assert one.high == math.inf
    assert one.low == pytest.approx(two.low)  # a one-sided 95% LB is the two-sided 90% low


def test_brier_skill_is_a_ratio_of_day_sums_not_a_mean_of_day_skills() -> None:
    days = [(1.0, 2.0), (9.0, 10.0)]  # skills 0.5 and 0.1; pooled 1 - 10/12
    ci = brier_skill_ci(days, block=1, resamples=200, seed=1)
    assert ci.point == pytest.approx(1 - 10 / 12)


def test_bootstrap_refuses_bad_parameters() -> None:
    with pytest.raises(ValueError):
        bootstrap_ci([], statistics.fmean, block=1, resamples=10, seed=1)
    with pytest.raises(ValueError):
        mean_ci([1.0, 2.0], block=0, resamples=10, seed=1)
    with pytest.raises(ValueError):
        mean_ci([1.0, 2.0], block=1, resamples=10, seed=1, level=1.0)


# --- sign test, Holm -------------------------------------------------------------------


def test_sign_test_twelve_of_fifteen() -> None:
    assert sign_test(12, 15) == pytest.approx(576 / 32768)


def test_sign_test_edges() -> None:
    assert sign_test(0, 10) == 1.0
    assert sign_test(10, 10) == pytest.approx(1 / 1024)
    with pytest.raises(ValueError):
        sign_test(11, 10)


def test_holm_textbook_example() -> None:
    assert holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])


def test_holm_caps_at_one_and_keeps_order_monotone() -> None:
    assert holm([0.5, 0.6]) == pytest.approx([1.0, 1.0])
    assert holm([]) == []


# --- deflated Sharpe, MinTRL -----------------------------------------------------------


def test_expected_max_factor_for_twenty_trials() -> None:
    assert expected_max_sharpe(20, 1.0) == pytest.approx(1.90, abs=0.01)
    assert expected_max_sharpe(1, 1.0) == 0.0


def test_deflated_sharpe_matches_bailey_lopez_de_prado_2014() -> None:
    # The paper's example: annualised SR 2.5 over 5 years of daily returns, N = 100 trials,
    # annualised V[SR] = 0.5, skew -3, kurtosis 10 -> SR0 ~ 0.1132 daily, DSR ~ 0.9004.
    sr, var = 2.5 / math.sqrt(250), 0.5 / 250
    assert expected_max_sharpe(100, var) == pytest.approx(0.1132, abs=5e-4)
    dsr = deflated_sharpe(sr, t=1250, skew=-3, kurt=10, n_trials=100, var_sr=var)
    assert dsr == pytest.approx(0.9004, abs=2e-3)


def test_more_trials_deflate_more() -> None:
    kw = {"t": 500, "skew": 0.0, "kurt": 3.0, "var_sr": 0.001}
    assert deflated_sharpe(0.1, n_trials=40, **kw) < deflated_sharpe(0.1, n_trials=5, **kw)


def test_min_track_record_length_for_daily_sr_point_one() -> None:
    assert min_track_record_length(0.1) == pytest.approx(273, abs=1)
    assert min_track_record_length(0.0) == math.inf


def test_psr_at_min_track_record_length_is_the_confidence() -> None:
    t = min_track_record_length(0.08, skew=-1, kurt=6, alpha=0.05)
    assert probabilistic_sharpe(0.08, 0.0, t=t, skew=-1, kurt=6) == pytest.approx(0.95)


# --- PBO (CSCV) ------------------------------------------------------------------------


def test_pbo_is_one_when_the_in_sample_winner_always_loses_out_of_sample() -> None:
    a = [2.0, 3.0, -1.0, -2.0]
    b = [-2.0, -1.0, 2.0, 3.0]
    result = pbo_cscv([[x, y] for x, y in zip(a, b, strict=True)], splits=2)
    assert result.pbo == 1.0
    assert result.combinations == 2


def test_pbo_is_zero_for_a_config_that_dominates_everywhere() -> None:
    rng = random.Random(11)
    rows = [[rng.gauss(1.0, 1), *(rng.gauss(0, 1) for _ in range(9))] for _ in range(320)]
    assert pbo_cscv(rows, splits=8).pbo == 0.0


def test_pbo_of_pure_noise_is_about_one_half() -> None:
    rng = random.Random(12)
    rows = [[rng.gauss(0, 1) for _ in range(10)] for _ in range(400)]
    assert pbo_cscv(rows, splits=10).pbo == pytest.approx(0.5, abs=0.15)


def test_pbo_refuses_bad_shapes() -> None:
    with pytest.raises(ValueError):
        pbo_cscv([[1.0, 2.0]] * 8, splits=3)  # odd split count
    with pytest.raises(ValueError):
        pbo_cscv([[1.0]] * 8, splits=2)  # one config
    with pytest.raises(ValueError):
        pbo_cscv([[1.0, 2.0]] * 3, splits=4)  # fewer rows than blocks


# --- ECE -------------------------------------------------------------------------------


def _calibrated(n: int, seed: int) -> tuple[list[float], list[bool]]:
    rng = random.Random(seed)
    ps = [rng.uniform(0.2, 0.8) for _ in range(n)]
    return ps, [rng.random() < p for p in ps]


def test_binned_ece_of_a_perfect_sample() -> None:
    assert ece_binned([0.0, 0.0, 1.0, 1.0], [False, False, True, True], bins=2) == 0.0
    assert ece_binned([0.5] * 4, [True] * 4, bins=2) == pytest.approx(0.5)


def test_expected_binned_ece_closed_form() -> None:
    assert expected_binned_ece(2000, bins=15, p=0.4) == pytest.approx(0.034, abs=1e-3)


def test_noise_floor_simulation_matches_the_closed_form() -> None:
    floor = ece_noise_floor([0.4], n_eff=2000, bins=15, sims=300, seed=13)
    assert floor.mean == pytest.approx(expected_binned_ece(2000, bins=15, p=0.4), rel=0.08)
    assert floor.p95 > floor.mean


def test_debiased_ece_is_near_zero_where_the_binned_one_is_not() -> None:
    ps, ys = _calibrated(3000, 14)
    assert ece_binned(ps, ys, bins=15) > 0.015
    sims = [ece_debiased(*_calibrated(3000, s), bins=15) ** 2 for s in range(60)]
    # The plug-in squared error of a calibrated model is ~ bins * p(1-p) / n (pure noise);
    # the debiased one removes that term, leaving only what the clip at 0 adds back.
    plug_in = 15 * 0.23 / 3000
    assert statistics.fmean(sims) < 0.3 * plug_in


def test_debiased_ece_recovers_a_known_miscalibration() -> None:
    rng = random.Random(15)
    ps = [rng.uniform(0.2, 0.7) for _ in range(20000)]
    ys = [rng.random() < p + 0.05 for p in ps]  # every forecast 0.05 too low
    assert ece_debiased(ps, ys, bins=15) == pytest.approx(0.05, abs=0.006)


def test_tied_forecasts_are_not_binned_by_outcome() -> None:
    ys = [i % 2 == 0 for i in range(40)]  # half happen; every forecast is 0.5
    assert ece_binned([0.5] * 40, ys, bins=4) == pytest.approx(0.0, abs=0.06)


def test_ece_refuses_too_few_samples_per_bin() -> None:
    with pytest.raises(ValueError):
        ece_debiased([0.5] * 10, [True] * 10, bins=10)


# --- ECE_sweep (protocol G2) -----------------------------------------------------------

HAND_P = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
HAND_Y = [False, False, True, False, True, True, True, True]


def test_the_sweep_stops_at_the_first_non_monotone_bin_count() -> None:
    # Equal-mass bins over 8 sorted rows; observed frequency per bin:
    #   b=2: 0.25, 1.0   b=3: 1/3, 1/2, 1   b=4: 0, 0.5, 1, 1   (all non-decreasing)
    #   b=5: 0, 1, 0.5, ...  -> falls, so the sweep keeps b=4.
    result = ece_sweep_binary(HAND_P, HAND_Y)
    assert result.bins == 4
    # b=4: |0.15-0| + |0.35-0.5| + |0.55-1| + |0.75-1|, each bin a quarter of the rows.
    assert result.ece == pytest.approx((0.15 + 0.15 + 0.45 + 0.25) / 4)


def test_outcomes_already_in_order_keep_one_row_per_bin() -> None:
    result = ece_sweep_binary([0.2, 0.4, 0.6, 0.8], [False, False, True, True])
    assert result.bins == 4
    assert result.ece == pytest.approx((0.2 + 0.4 + 0.4 + 0.2) / 4)


def test_ece_sweep_is_the_class_wise_mean_of_one_vs_rest() -> None:
    rng = random.Random(21)
    probs = [_simplex(rng) for _ in range(300)]
    labels = [rng.randrange(3) for _ in range(300)]
    per_class = [
        ece_sweep_binary([p[c] for p in probs], [y == c for y in labels]).ece for c in range(3)
    ]
    assert ece_sweep(probs, labels) == pytest.approx(statistics.fmean(per_class))


def _simplex(rng: random.Random) -> list[float]:
    raw = [rng.gammavariate(2.0, 1.0) for _ in range(3)]
    return [x / sum(raw) for x in raw]


def _draw(probs: list[list[float]], rng: random.Random) -> list[int]:
    return [rng.choices(range(3), weights=p)[0] for p in probs]


def test_a_calibrated_model_sits_inside_its_consistency_floor() -> None:
    rng = random.Random(22)
    probs = [_simplex(rng) for _ in range(2000)]
    labels = _draw(probs, rng)  # outcomes drawn from the forecasts: perfectly calibrated
    floor = ece_noise_floor_consistency(probs, n_resamples=60, seed=23)
    assert floor.quantile(0.05) <= ece_sweep(probs, labels) <= floor.quantile(0.99)
    assert floor.q50 < 0.03


def test_injected_miscalibration_clears_the_floor_plus_the_margin() -> None:
    rng = random.Random(24)
    truth = [_simplex(rng) for _ in range(2000)]
    labels = _draw(truth, rng)
    flat = [[0.5 * p + 0.5 / 3 for p in row] for row in truth]  # underconfident forecasts
    floor = ece_noise_floor_consistency(flat, n_resamples=60, seed=25)
    assert ece_sweep(flat, labels) > floor.q50 + 0.02


def test_the_consistency_floor_is_seeded_and_sorted() -> None:
    probs = [_simplex(random.Random(s)) for s in range(200)]
    a = ece_noise_floor_consistency(probs, n_resamples=20, seed=5)
    assert a == ece_noise_floor_consistency(probs, n_resamples=20, seed=5)
    assert a.draws == sorted(a.draws) and len(a.draws) == 20


def test_ece_sweep_refuses_bad_inputs() -> None:
    with pytest.raises(ValueError):
        ece_sweep([[0.5, 0.6, 0.1]] * 4, [0] * 4)  # does not sum to 1
    with pytest.raises(ValueError):
        ece_sweep([[0.5, 0.5]] * 4, [2] * 4)  # label outside the classes
    with pytest.raises(ValueError):
        ece_sweep_binary([0.5], [True])  # one row


# --- power -----------------------------------------------------------------------------


def test_power_at_zero_effect_is_the_false_positive_rate() -> None:
    assert mean_lb_power(0.0, 1.0, 100, sims=4000, seed=16) == pytest.approx(0.05, abs=0.012)


def test_power_matches_the_normal_closed_form() -> None:
    effect, sd, n = 0.25, 1.0, 100  # z = 2.5: power = Phi(2.5 - 1.645) ~ 0.80
    expected = statistics.NormalDist().cdf(effect * math.sqrt(n) / sd - 1.6449)
    assert mean_lb_power(effect, sd, n, sims=4000, seed=17) == pytest.approx(expected, abs=0.025)


def test_simulated_power_runs_any_gate() -> None:
    rate = simulated_power(lambda rng: rng.random(), lambda u: u < 0.3, sims=5000, seed=18)
    assert rate == pytest.approx(0.3, abs=0.02)


# --- A/A noise floor, best-of-N --------------------------------------------------------


def test_aa_noise_floor_from_seed_variation() -> None:
    floor = aa_noise_floor([1.0, 2.0, 3.0, 4.0, 5.0])
    assert floor == pytest.approx(1.96 * math.sqrt(2) * statistics.stdev([1, 2, 3, 4, 5]))
    with pytest.raises(ValueError):
        aa_noise_floor([1.0])


def test_compare_to_floor() -> None:
    assert compare_to_floor(1.5, 1.0, floor=0.4) is Comparison.BETTER
    assert compare_to_floor(0.5, 1.0, floor=0.4) is Comparison.WORSE
    assert compare_to_floor(1.3, 1.0, floor=0.4) is Comparison.UNCONFIRMED


def test_best_of_n_reports_n_and_the_lift_over_a_fresh_baseline() -> None:
    result = best_of_n(
        {"B0": 0.10, "A2": 0.18, "A3": 0.12}, baseline=0.11, n_trials=5, noise_floor=0.05
    )
    assert (result.best, result.n_trials) == ("A2", 5)
    assert result.lift == pytest.approx(0.07)
    assert result.confirmed


def test_best_of_n_below_the_floor_is_unconfirmed_and_n_cannot_be_understated() -> None:
    result = best_of_n({"A": 0.13}, baseline=0.11, n_trials=1, noise_floor=0.05)
    assert not result.confirmed
    with pytest.raises(ValueError):
        best_of_n({"A": 0.1, "B": 0.2}, baseline=0.0, n_trials=1, noise_floor=0.0)


@pytest.mark.parametrize(
    "call",
    [
        lambda: expected_max_sharpe(0, 1.0),
        lambda: expected_max_sharpe(5, -0.1),
        lambda: probabilistic_sharpe(0.1, 0.0, t=1, skew=0.0, kurt=3.0),
        lambda: probabilistic_sharpe(1.0, 0.0, t=100, skew=5.0, kurt=3.0),  # SR variance <= 0
        lambda: mean_lb_power(0.1, 1.0, 1, seed=1),
        lambda: mean_lb_power(0.1, 0.0, 30, seed=1),
        lambda: ece_noise_floor_consistency([[0.5, 0.5]] * 4, n_resamples=0, seed=1),
        lambda: simulated_power(lambda rng: rng.random(), lambda x: x > 0.5, sims=0, seed=1),
        lambda: pbo_cscv([[1.0, 2.0], [1.0]] * 4, splits=2),  # not rectangular
        lambda: pbo_cscv([[1.0, 2.0]] * 8, splits=0),
        lambda: mean_ci([1.0, 2.0], block=1, resamples=0, seed=1),
        lambda: best_of_n({}, baseline=0.0, n_trials=0, noise_floor=0.0),
        lambda: ece_noise_floor([], n_eff=100, bins=10, seed=1),
        lambda: ece_noise_floor([0.5, 1.5], n_eff=100, bins=10, seed=1),
        lambda: ece_noise_floor([0.5], n_eff=10, bins=10, seed=1),  # fewer than 2 per bin
    ],
)
def test_guards_refuse_bad_parameters(call: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        call()
