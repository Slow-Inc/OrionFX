"""M04 slice B: adaptive ensemble (#47; PRE-03: weights from validation, regime/data
quality; correlated inputs not double counted; PRE-04/AT-17 lineages kept apart)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.contracts import Confidence, ParameterBound, PredictionGenerated
from app.core.enums import DataQuality, Direction, ForecastLineage, Timeframe
from app.prediction.card import ModelCard
from app.prediction.ensemble import EnsembleResult, ensemble

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)


def _card(
    model_id: str, brier: str, *, lineage: ForecastLineage = ForecastLineage.CHART_ONLY
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
        horizon_bars=3,
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
    refs: tuple[str, ...] = ("snap@T0",),
    quality: DataQuality = DataQuality.OK,
    until: timedelta = timedelta(hours=1),
) -> PredictionGenerated:
    return PredictionGenerated(
        prediction_id=f"{card.model_id}-{direction}-{score}",
        model_id=card.model_id,
        model_version=card.model_version,
        lineage=card.lineage,
        horizon=card.horizon,
        horizon_bars=card.horizon_bars,
        direction=direction,
        probability=Confidence(value=D(score), semantics="s"),
        data_quality=quality,
        input_refs=list(refs),
        generated_at=T0,
        valid_until=T0 + until,
    )


def _run(members: list[tuple[PredictionGenerated, ModelCard]]) -> EnsembleResult:
    return ensemble(members, now=NOW, lineage=ForecastLineage.CHART_ONLY)


A, B, C = _card("xgb", "0.20"), _card("lgbm", "0.23"), _card("weak", "0.26")


def test_members_are_weighted_by_skill_over_the_baseline() -> None:
    result = _run([(_pred(A, Direction.UP, "0.6"), A), (_pred(B, Direction.DOWN, "0.6"), B)])
    assert result.prediction is not None and result.prediction.direction is Direction.UP
    assert result.weights == {"xgb-UP-0.6": D("0.05"), "lgbm-DOWN-0.6": D("0.02")}


def test_a_member_no_better_than_the_baseline_gets_no_weight() -> None:
    result = _run([(_pred(C, Direction.UP, "0.9"), C)])
    assert result.prediction is None and "no eligible member" in result.reason


def test_degraded_data_halves_the_weight() -> None:
    degraded = _pred(A, Direction.UP, "0.6", quality=DataQuality.DEGRADED)
    result = _run([(degraded, A), (_pred(B, Direction.DOWN, "0.6"), B)])
    assert result.weights["xgb-UP-0.6"] == D("0.025")


def test_correlated_members_count_once() -> None:
    a1 = _pred(A, Direction.UP, "0.6")
    a2 = _pred(A, Direction.UP, "0.7")  # same model, same inputs: not independent
    result = _run([(a1, A), (a2, A), (_pred(B, Direction.DOWN, "0.9"), B)])
    assert len(result.weights) == 2


def test_lineages_are_never_mixed() -> None:
    news = _card("news", "0.20", lineage=ForecastLineage.NEWS_ENHANCED)
    result = _run([(_pred(news, Direction.DOWN, "0.9"), news), (_pred(A, Direction.UP, "0.6"), A)])
    assert result.prediction is not None and result.prediction.direction is Direction.UP
    assert result.prediction.lineage is ForecastLineage.CHART_ONLY
    assert set(result.weights) == {"xgb-UP-0.6"}


def test_expired_or_unusable_members_are_left_out() -> None:
    old = _pred(A, Direction.UP, "0.6", until=timedelta(seconds=1))
    stale = _pred(B, Direction.DOWN, "0.6", quality=DataQuality.STALE)
    result = _run([(old, A), (stale, B)])
    assert result.prediction is None


def test_output_is_labelled_uncalibrated_with_member_lineage_and_earliest_expiry() -> None:
    a = _pred(A, Direction.UP, "0.6", until=timedelta(minutes=30))
    b = _pred(B, Direction.UP, "0.8")
    p = _run([(a, A), (b, B)]).prediction
    assert p is not None
    assert p.input_refs == [a.prediction_id, b.prediction_id]
    assert p.valid_until == T0 + timedelta(minutes=30)
    assert not p.probability.calibrated and "weighted" in p.probability.semantics
    # (0.05*0.6 + 0.02*0.8) / 0.07
    assert p.probability.value == (D("0.05") * D("0.6") + D("0.02") * D("0.8")) / D("0.07")


def test_a_weighted_tie_gives_no_forecast() -> None:
    twin = _card("twin", "0.20")
    result = _run([(_pred(A, Direction.UP, "0.6"), A), (_pred(twin, Direction.DOWN, "0.6"), twin)])
    assert result.prediction is None and "tie" in result.reason


def test_mismatched_horizons_are_not_combined() -> None:
    other = A.model_copy(update={"model_id": "short", "horizon_bars": 1})
    result = _run(
        [(_pred(A, Direction.UP, "0.6"), A), (_pred(other, Direction.DOWN, "0.9"), other)]
    )
    assert result.prediction is None and "horizon" in result.reason
