"""M04 slice B: adaptive ensemble (#47; PRE-03: weights from validation and data
quality, correlated inputs not double counted; PRE-04/AT-17: lineages kept apart,
semantics stated)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.contracts import Confidence, ParameterBound, PredictionGenerated
from app.core.enums import DataQuality, Direction, ForecastLineage, Timeframe
from app.prediction.card import ModelCard
from app.prediction.ensemble import EnsembleResult, WiringError, ensemble

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)
CHART = ForecastLineage.CHART_ONLY


def _card(
    model_id: str, brier: str, *, lineage: ForecastLineage = CHART, bars: int = 3
) -> ModelCard:
    return ModelCard(
        model_id=model_id,
        model_version="1.0.0",
        artifact_hash="sha256:" + "0" * 64,
        lineage=lineage,
        feature_schema="m02.features.v1",
        required_features=["H1.rsi_14.rsi"],
        feature_ranges={"H1.rsi_14.rsi": ParameterBound(low=D(0), high=D(100))},
        horizon=Timeframe.H1,
        horizon_bars=bars,
        semantics="uncalibrated model score",
        calibrated=False,
        validation={"brier": D(brier), "baseline_brier": D("0.25")},
        evidence_refs=["x02:wf"],
    )


def _pred(
    card: ModelCard,
    direction: Direction,
    score: str,
    *,
    refs: tuple[str, ...] | None = None,
    quality: DataQuality = DataQuality.OK,
    until: timedelta = timedelta(hours=1),
    at: datetime = T0,
) -> PredictionGenerated:
    return PredictionGenerated(
        prediction_id=f"{card.model_id}-{direction}-{score}-{at.second}",
        model_id=card.model_id,
        model_version=card.model_version,
        lineage=card.lineage,
        horizon=card.horizon,
        horizon_bars=card.horizon_bars,
        direction=direction,
        probability=Confidence(value=D(score), semantics="s"),
        data_quality=quality,
        input_refs=list(refs or (f"inputs-of-{card.model_id}",)),
        generated_at=at,
        valid_until=at + until,
    )


def _run(members: list[tuple[PredictionGenerated, ModelCard]]) -> EnsembleResult:
    return ensemble(members, now=NOW, lineage=CHART, horizon=Timeframe.H1, horizon_bars=3)


A, B, C = _card("xgb", "0.20"), _card("lgbm", "0.23"), _card("weak", "0.26")


def test_members_are_weighted_by_skill_over_the_baseline() -> None:
    a, b = _pred(A, Direction.UP, "0.6"), _pred(B, Direction.DOWN, "0.6")
    result = _run([(a, A), (b, B)])
    assert result.prediction is not None and result.prediction.direction is Direction.UP
    assert result.weights == {a.prediction_id: D("0.05"), b.prediction_id: D("0.02")}


def test_a_member_no_better_than_the_baseline_gets_no_weight() -> None:
    result = _run([(_pred(C, Direction.UP, "0.9"), C)])
    assert result.prediction is None and "no eligible member" in result.reason


def test_degraded_data_halves_the_weight() -> None:
    degraded = _pred(A, Direction.UP, "0.6", quality=DataQuality.DEGRADED)
    result = _run([(degraded, A), (_pred(B, Direction.DOWN, "0.6"), B)])
    assert result.weights[degraded.prediction_id] == D("0.025")
    assert result.prediction is not None
    assert result.prediction.data_quality is DataQuality.DEGRADED


def test_repeats_of_one_model_keep_only_the_most_recent_in_any_order() -> None:
    old = _pred(A, Direction.UP, "0.6", refs=("snap",))
    new = _pred(A, Direction.DOWN, "0.7", refs=("snap",), at=T0 + timedelta(seconds=1))
    for order in ([(old, A), (new, A)], [(new, A), (old, A)]):
        result = _run(order)
        assert list(result.weights) == [new.prediction_id]
        assert result.prediction is not None and result.prediction.direction is Direction.DOWN


def test_models_reading_the_same_inputs_are_one_vote() -> None:
    """Two boosted models on one snapshot must not outvote one independent model."""
    up_a = _pred(A, Direction.UP, "0.6", refs=("snap@T0",))
    up_b = _pred(B, Direction.UP, "0.8", refs=("snap@T0",))
    indep_card = _card("indep", "0.21")
    down = _pred(indep_card, Direction.DOWN, "0.9", refs=("other@T0",))
    result = _run([(up_a, A), (up_b, B), (down, indep_card)])
    assert result.prediction is not None and result.prediction.direction is Direction.DOWN


def test_lineages_are_never_mixed() -> None:
    news = _card("news", "0.20", lineage=ForecastLineage.NEWS_ENHANCED)
    a = _pred(A, Direction.UP, "0.6")
    result = _run([(_pred(news, Direction.DOWN, "0.9"), news), (a, A)])
    assert result.prediction is not None and result.prediction.direction is Direction.UP
    assert result.prediction.lineage is CHART and set(result.weights) == {a.prediction_id}


def test_other_horizons_are_left_out_not_fatal() -> None:
    short = _card("short", "0.20", bars=1)
    a = _pred(A, Direction.UP, "0.6")
    result = _run([(a, A), (_pred(short, Direction.DOWN, "0.9"), short)])
    assert result.prediction is not None and set(result.weights) == {a.prediction_id}


def test_expired_or_unusable_members_are_left_out() -> None:
    old = _pred(A, Direction.UP, "0.6", until=timedelta(seconds=1))
    stale = _pred(B, Direction.DOWN, "0.6", quality=DataQuality.STALE)
    assert _run([(old, A), (stale, B)]).prediction is None


def test_output_states_its_horizon_lineage_inputs_and_earliest_expiry() -> None:
    a = _pred(A, Direction.UP, "0.6", until=timedelta(minutes=30))
    b = _pred(B, Direction.UP, "0.8")
    p = _run([(a, A), (b, B)]).prediction
    assert p is not None
    assert p.input_refs == [a.prediction_id, b.prediction_id]
    assert p.valid_until == T0 + timedelta(minutes=30)
    assert not p.probability.calibrated and "3 H1 bars" in p.probability.semantics
    assert p.probability.value == (D("0.05") * D("0.6") + D("0.02") * D("0.8")) / D("0.07")


def test_a_weighted_tie_gives_no_forecast() -> None:
    twin = _card("twin", "0.20")
    result = _run([(_pred(A, Direction.UP, "0.6"), A), (_pred(twin, Direction.DOWN, "0.6"), twin)])
    assert result.prediction is None and "tie" in result.reason


@pytest.mark.parametrize(
    "card",
    [
        A.model_copy(update={"model_version": "2.0.0"}),
        _card("xgb", "0.20", bars=1),
        _card("xgb", "0.20", lineage=ForecastLineage.NEWS_ENHANCED),
    ],
)
def test_a_card_that_does_not_describe_the_prediction_is_a_wiring_error(card: ModelCard) -> None:
    with pytest.raises(WiringError):
        _run([(_pred(A, Direction.UP, "0.6"), card)])
