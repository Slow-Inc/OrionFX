"""Independent recomputation of a run's headline metrics from its per-prediction outputs
(#132; training-loop.md §6 rule 6). Expected values are worked by hand."""

import math

import pytest

from app.validation.recompute import Prediction, recheck, recompute

PREDS = [
    Prediction((0.7, 0.2, 0.1), 0),  # right; Brier 0.09 + 0.04 + 0.01 = 0.14
    Prediction((0.2, 0.5, 0.3), 2),  # wrong; Brier 0.04 + 0.25 + 0.49 = 0.78
]
PRIOR = (0.5, 0.25, 0.25)  # Brier 0.25+0.0625+0.0625 = 0.375 and 0.25+0.0625+0.5625 = 0.875


def test_recompute_by_hand() -> None:
    got = recompute(PREDS, prior=PRIOR)
    assert got["brier"] == pytest.approx(0.46)
    assert got["accuracy"] == pytest.approx(0.5)
    assert got["log_loss"] == pytest.approx(-(math.log(0.7) + math.log(0.3)) / 2)
    assert got["brier_skill"] == pytest.approx(1 - 0.92 / 1.25)


def test_without_a_prior_there_is_no_brier_skill() -> None:
    assert "brier_skill" not in recompute(PREDS)


def test_matching_logged_metrics_pass() -> None:
    result = recheck(PREDS, {"brier": 0.46, "accuracy": 0.5, "expectancy": 1.2}, prior=PRIOR)
    assert result.ok
    assert result.checked == ["accuracy", "brier"]
    assert result.unchecked == ["expectancy"]


def test_a_mismatch_is_flagged() -> None:
    result = recheck(PREDS, {"brier": 0.40, "accuracy": 0.5})
    assert not result.ok
    assert result.mismatches == {"brier": (0.40, pytest.approx(0.46))}


def test_the_tolerance_is_absolute() -> None:
    assert recheck(PREDS, {"brier": 0.4601}, tolerance=1e-3).ok
    assert not recheck(PREDS, {"brier": 0.4601}, tolerance=1e-5).ok


def test_nothing_checked_is_not_ok() -> None:
    assert not recheck(PREDS, {"expectancy": 1.0}).ok


def test_bad_outputs_are_refused() -> None:
    with pytest.raises(ValueError):
        recompute([])
    with pytest.raises(ValueError):
        recompute([Prediction((0.5, 0.6), 0)])  # does not sum to 1
    with pytest.raises(ValueError):
        recompute([Prediction((0.5, 0.5), 2)])  # label out of range
    with pytest.raises(ValueError):
        recompute(PREDS, prior=(0.5, 0.5))  # prior of the wrong width
