"""A forecast carries the calibrated triple-barrier class probabilities (#129): P(UP),
P(DOWN), P(FLAT) of the one label both sides share, under the label version the model
was calibrated on. M05 prices EV from them, so they are emitted only
from a calibrated card that names its label version, and a model whose three scores do
not sum to 1 is refused, never repaired."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.bet import LABEL_VERSION
from app.core.contracts import ClassProbabilities, FeatureSnapshot
from app.core.enums import DataQuality, Direction, ForecastLineage, Mode, Timeframe
from app.prediction.card import ModelCard
from app.prediction.engine import Refused, load_baseline, predict

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)
KEY = "H1.rsi_14.rsi"


def _card(**over: object) -> ModelCard:
    base: dict[str, object] = {
        "model_id": "tb", "model_version": "1.0.0", "artifact_hash": None,
        "lineage": ForecastLineage.CHART_ONLY, "feature_schema": "m02.features.v2",
        "required_features": [KEY], "feature_ranges": {}, "horizon": Timeframe.H1,
        "horizon_bars": 1, "semantics": "calibrated P(outcome) of m07.labels.tb.v2",
        "calibrated": True, "validation": {}, "evidence_refs": ["calibration-report"],
        "label_version": LABEL_VERSION,
    }  # fmt: skip
    base.update(over)
    return ModelCard.model_validate(base)


class ThreeClass:
    def __init__(self, scores: Mapping[Direction, str], **card: object) -> None:
        self.card = _card(**card)
        self._scores = {d: D(s) for d, s in scores.items()}

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        return self._scores


SNAP = FeatureSnapshot(symbol="XAUUSD", feature_schema="m02.features.v2", values={KEY: D(60)},
                       quality=DataQuality.OK, as_of=T0, available_at=T0 + timedelta(seconds=1),
                       valid_until=T0 + timedelta(minutes=5))  # fmt: skip
SCORES = {Direction.UP: "0.5", Direction.DOWN: "0.2", Direction.NEUTRAL: "0.3"}


def _predict(model: ThreeClass):  # type: ignore[no-untyped-def]
    return predict(load_baseline(model, mode=Mode.DEMO), SNAP, now=NOW)


def test_a_calibrated_three_class_model_emits_its_class_probabilities() -> None:
    p = _predict(ThreeClass(SCORES))
    assert p.direction is Direction.UP and p.probability.value == D("0.5")
    assert p.class_probabilities == ClassProbabilities(
        label_version=LABEL_VERSION, up=D("0.5"), down=D("0.2"), flat=D("0.3")
    )


@pytest.mark.parametrize(
    "card",
    [
        {"calibrated": False, "evidence_refs": []},  # an uncalibrated score is not priced
        {"label_version": None},  # calibrated on what? not priced
    ],
)
def test_without_a_calibrated_label_version_there_are_none(card: dict[str, object]) -> None:
    assert _predict(ThreeClass(SCORES, **card)).class_probabilities is None


def test_a_labelled_model_without_three_scores_is_refused() -> None:
    with pytest.raises(Refused, match="UP, DOWN and NEUTRAL"):
        _predict(ThreeClass({Direction.UP: "0.6", Direction.DOWN: "0.4"}))


def test_a_model_calibrated_on_another_label_is_refused_at_load() -> None:
    with pytest.raises(Refused, match="calibrated on another"):
        load_baseline(ThreeClass(SCORES, label_version="m07.labels.tb.v0"), mode=Mode.DEMO)


def test_a_flat_leader_still_carries_the_triple() -> None:
    flat = {Direction.UP: "0.2", Direction.DOWN: "0.1", Direction.NEUTRAL: "0.7"}
    p = _predict(ThreeClass(flat))
    assert p.direction is Direction.NEUTRAL  # the label is the same for both sides
    assert p.class_probabilities is not None and p.class_probabilities.flat == D("0.7")


def test_three_scores_that_do_not_sum_to_one_are_refused() -> None:
    bad = {Direction.UP: "0.5", Direction.DOWN: "0.2", Direction.NEUTRAL: "0.2"}
    with pytest.raises(Refused, match="sum"):
        _predict(ThreeClass(bad))


def test_the_contract_refuses_probabilities_that_do_not_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        ClassProbabilities(label_version=LABEL_VERSION, up=D("0.5"), down=D("0.5"),
                           flat=D("0.1"))  # fmt: skip
