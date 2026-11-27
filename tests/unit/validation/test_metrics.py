"""X02 slice A: validation metrics and chronological walk-forward splits (#66; VAL-02, VAL-04).
Every metric carries its sample size; an empty sample is None, never 0."""

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.enums import Direction
from app.validation.metrics import (
    brier,
    direction_accuracy,
    expectancy,
    mae,
    max_drawdown,
    profit_factor,
    reliability,
    rmse,
    target_hit_rate,
    win_rate,
)
from app.validation.splits import walk_forward

D = Decimal
UP, DOWN = Direction.UP, Direction.DOWN


def test_direction_accuracy_counts_matches_over_decided_outcomes() -> None:
    m = direction_accuracy([UP, UP, DOWN, UP], [UP, DOWN, DOWN, Direction.NEUTRAL])
    assert m.n == 3 and m.value == D(2) / D(3)  # the NEUTRAL outcome is not counted
    assert direction_accuracy([], []).value is None


def test_brier_is_the_mean_squared_probability_error() -> None:
    m = brier([D("0.8"), D("0.6"), D("0.3")], [True, False, False])
    # (0.2^2 + 0.6^2 + 0.3^2) / 3 = (0.04 + 0.36 + 0.09) / 3
    assert m.n == 3 and m.value == D("0.49") / 3
    with pytest.raises(ValueError):
        brier([D("0.5")], [True, False])


def test_reliability_bins_compare_forecast_to_frequency() -> None:
    probs = [D("0.1"), D("0.15"), D("0.8"), D("0.9")]
    bins = reliability(probs, [False, True, True, True], bins=2)
    low, high = bins
    assert (low.n, low.mean_probability, low.frequency) == (2, D("0.125"), D("0.5"))
    assert (high.n, high.mean_probability, high.frequency) == (2, D("0.85"), D(1))
    assert reliability([], [], bins=2)[0].frequency is None
    top = reliability([D(1)], [True], bins=2)[1]  # p = 1 belongs to the last bin
    assert top.n == 1 and top.frequency == D(1)


def test_mae_and_rmse() -> None:
    pred, actual = [D(1), D(2), D(4)], [D(1), D(3), D(2)]
    assert mae(pred, actual).value == D(1)  # (0 + 1 + 2) / 3
    assert rmse(pred, actual).value == (D(5) / 3).sqrt()


TRADES = [D(10), D(-4), D(6), D(-8), D(3)]


def test_expectancy_win_rate_and_profit_factor() -> None:
    assert expectancy(TRADES).value == D("1.4") and expectancy(TRADES).n == 5
    assert win_rate(TRADES).value == D("0.6")
    assert profit_factor(TRADES).value == D(19) / D(12)
    assert profit_factor([D(1), D(2)]).value is None  # no losses: undefined, not infinite
    assert win_rate([]).value is None and expectancy([]).value is None


def test_max_drawdown_is_the_deepest_peak_to_trough_fall() -> None:
    # equity 10, 6, 12, 4, 7 -> peak 12 then 4: drawdown 8
    assert max_drawdown(TRADES).value == D(8)
    assert max_drawdown([D(-5), D(-3)]).value == D(8)  # from the starting 0
    assert max_drawdown([D(1), D(2)]).value == D(0)


def test_target_hit_rate() -> None:
    assert target_hit_rate([True, False, True, True]).value == D("0.75")


# --- walk-forward -----------------------------------------------------------------------------

T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


def test_folds_are_chronological_embargoed_and_never_touch_the_holdout() -> None:
    plan = walk_forward(T0, T0 + 100 * DAY, train=30 * DAY, test=10 * DAY,
                        embargo=2 * DAY, holdout=20 * DAY)  # fmt: skip
    folds, holdout = plan.folds, plan.holdout
    assert (holdout.start, holdout.end) == (T0 + 80 * DAY, T0 + 100 * DAY)
    assert len(folds) == 4  # the last tests days 62-72
    for f in folds:
        assert f.train_end + 2 * DAY == f.test_start  # the embargo gap
        assert f.test_end + 2 * DAY <= holdout.start  # an embargo before the holdout too
    tests = [(f.test_start, f.test_end) for f in folds]
    assert all(a[1] <= b[0] for a, b in itertools.pairwise(tests))  # ordered


def test_walk_forward_refuses_impossible_or_empty_plans() -> None:
    with pytest.raises(ValueError):
        walk_forward(T0, T0 + 30 * DAY, train=30 * DAY, test=10 * DAY, embargo=DAY,
                     holdout=10 * DAY)  # fmt: skip
    with pytest.raises(ValueError):
        walk_forward(T0, T0 + 100 * DAY, train=-DAY, test=10 * DAY, embargo=DAY, holdout=DAY)


# --- review fixes ---


def test_a_certain_forecast_is_never_dropped_whatever_the_bin_count() -> None:
    for bins in (2, 3, 6, 7, 9):
        assert sum(b.n for b in reliability([D(1), D(0)], [True, False], bins=bins)) == 2


def test_out_of_range_probabilities_and_bad_bins_are_refused() -> None:
    with pytest.raises(ValueError):
        brier([D("1.2")], [True])
    with pytest.raises(ValueError):
        reliability([D("-0.1")], [False], bins=2)
    with pytest.raises(ValueError):
        reliability([D("0.5")], [True], bins=0)


def test_abstaining_and_break_even_are_not_counted_as_misses() -> None:
    assert direction_accuracy([Direction.NEUTRAL, UP], [UP, UP]).value == D(1)
    assert direction_accuracy([Direction.NEUTRAL, UP], [UP, UP]).n == 1
    assert win_rate([D(5), D(0)]).value == D(1) and win_rate([D(5), D(0)]).n == 1


def test_a_reversed_range_is_reported_as_such() -> None:
    with pytest.raises(ValueError, match="start must be before end"):
        walk_forward(T0 + DAY, T0, train=DAY, test=DAY, embargo=DAY, holdout=DAY)


def test_the_embargo_also_guards_the_holdout_boundary() -> None:
    # without the second embargo a 4th fold (test days 68-78) would end 2 days before
    # the holdout, inside the 8-day embargo
    plan = walk_forward(T0, T0 + 100 * DAY, train=30 * DAY, test=10 * DAY, embargo=8 * DAY,
                        holdout=20 * DAY)  # fmt: skip
    assert len(plan.folds) == 3
    assert plan.folds[-1].test_end + 8 * DAY <= plan.holdout.start
