"""The #127 seams: an evaluation load for scoring history, one reviewed adapter that reads
hash-pinned JSON weights, and the training cutoff. An evaluation-loaded model needs no
approval, so no live path may accept it."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support import tracer

from app.core.bet import LABEL_VERSION
from app.core.contracts import FeatureSnapshot, ParameterBound, sha256_ref
from app.core.enums import DataQuality, Direction, ForecastLineage, Mode, Timeframe
from app.decision.fusion import Costs
from app.learning.bars import Horizon, Provenance
from app.learning.experience import ExperienceLog
from app.market_data.store import CandleStore
from app.prediction.adapters import barrier_softmax
from app.prediction.baseline import BASELINE
from app.prediction.card import ModelCard
from app.prediction.engine import (
    EvaluationPredictor,
    Refused,
    load_baseline,
    load_for_evaluation,
    load_model,
    predict,
    predict_for_evaluation,
)
from app.runtime.loop import DecisionLoop
from app.runtime.market_recorder import MarketRecorder
from app.storage.artifacts import ArtifactLoader
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry
from app.validation.replay import EvaluationEvidence

D = Decimal
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=5)
CUTOFF = T0 - timedelta(days=1)
H1_DIR, M15_DIR = "H1.direction_10.direction", "M15.direction_10.direction"
MODULE, ADAPTER_HASH = tracer.ADAPTER, tracer.ADAPTER_HASH
WEIGHTS = json.dumps({"features": [H1_DIR, M15_DIR], "intercept": ["0", "0", "0"],
                      "coef": [["1.5", "1.0"], ["-1.5", "-1.0"], ["0", "0"]],
                      "temperature": "1"}).encode()  # fmt: skip


def _card(weights: bytes = WEIGHTS, **over: object) -> ModelCard:
    base: dict[str, object] = {
        "model_id": "tracer-logit", "model_version": "1", "artifact_hash": ADAPTER_HASH,
        "lineage": ForecastLineage.CHART_ONLY, "feature_schema": "m02.features.v2",
        "required_features": [H1_DIR, M15_DIR],
        "feature_ranges": {k: ParameterBound(low=D(-1), high=D(1)) for k in (H1_DIR, M15_DIR)},
        "horizon": Timeframe.H1, "horizon_bars": 1,
        "semantics": "Platt-calibrated P(close up after 1 H1 bar)", "calibrated": True,
        "validation": {}, "evidence_refs": ["calibration:ds@1:validation"],
        "training_cutoff": CUTOFF, "weights_hash": sha256_ref(weights),
    }  # fmt: skip
    base.update(over)
    return ModelCard.model_validate(base)


def _snapshot(as_of: datetime = T0, **values: Decimal | None) -> FeatureSnapshot:
    base: dict[str, Decimal | None] = {H1_DIR: D(1), M15_DIR: D(1)}
    base.update(values)
    return FeatureSnapshot(symbol="XAUUSD", feature_schema="m02.features.v2", values=base,
                           quality=DataQuality.OK, as_of=as_of,
                           available_at=as_of + timedelta(seconds=1),
                           valid_until=as_of + timedelta(minutes=5))  # fmt: skip


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "eval.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def registry(conn: sqlite3.Connection) -> ArtifactRegistry:
    """Registered long after T0 and never approved for any mode."""
    reg = ArtifactRegistry(conn, clock=lambda: T0 + timedelta(days=300))
    reg.register("model", _card().manifest(), actor="m07")
    return reg


def _evaluate(registry: ArtifactRegistry, card: ModelCard | None = None,
              weights: bytes = WEIGHTS) -> EvaluationPredictor:  # fmt: skip
    loader = ArtifactLoader(registry, allowed_packages=("app.prediction.adapters",))
    return load_for_evaluation(loader, registry, card=card or _card(), weights=weights,
                               module=MODULE)  # fmt: skip


def test_a_model_registered_today_scores_history_without_any_approval(
    registry: ArtifactRegistry,
) -> None:
    loader = ArtifactLoader(registry, allowed_packages=("app.prediction.adapters",))
    with pytest.raises(Refused):  # the live load is point-in-time on an approval it lacks
        load_model(loader, registry, model_id="tracer-logit", version="1", module=MODULE,
                   mode=Mode.SHADOW, now=NOW)  # fmt: skip
    p = predict_for_evaluation(_evaluate(registry), _snapshot(), now=NOW)
    assert p.direction is Direction.UP and p.probability.calibrated
    e = [D("2.5").exp(), D("-2.5").exp(), D(1)]  # softmax of (2.5, -2.5, 0)
    assert abs(p.probability.value - e[0] / sum(e)) < D("1e-20")


def test_no_live_path_accepts_an_evaluation_predictor(
    conn: sqlite3.Connection, registry: ArtifactRegistry
) -> None:
    ev = _evaluate(registry)
    with pytest.raises(Refused, match="loaded predictor"):
        predict(ev, _snapshot(), now=NOW)  # type: ignore[arg-type]
    candles = CandleStore(conn, dataset_version="v1")
    for provenance in Provenance:
        with pytest.raises(Refused, match="evaluation"):
            MarketRecorder(conn, candles=candles, symbol="XAUUSD", timeframe=Timeframe.M5,
                           context_timeframes=[], horizons=[Horizon(Timeframe.M5, 1)],
                           provenance=provenance, predictors=[ev])  # type: ignore[list-item]  # fmt: skip
    with pytest.raises(Refused, match="evaluation"):
        DecisionLoop(conn, candles=candles, symbol="XAUUSD", timeframe=Timeframe.M5,
                     context_timeframes=[], strategies=[], registry=registry,
                     evidence=EvaluationEvidence(ev),  # type: ignore[arg-type]
                     quote=lambda now: None,  # type: ignore[arg-type,return-value]
                     costs=Costs(D(100), D(0)),
                     experiences=ExperienceLog(conn, candles))  # fmt: skip
    with pytest.raises(Refused, match="load_for_evaluation"):
        EvaluationPredictor(ev.predictor, object())


def test_the_adapter_refuses_weights_that_are_not_the_pinned_ones(
    registry: ArtifactRegistry,
) -> None:
    tampered = WEIGHTS.replace(b'"1.5"', b'"9.5"')
    with pytest.raises(Refused, match="weights"):
        _evaluate(registry, weights=tampered)
    with pytest.raises(ValueError, match="weights"):  # the adapter checks for itself too
        barrier_softmax.build(_card(), tampered)


def test_a_card_that_is_not_the_registered_one_is_refused(registry: ArtifactRegistry) -> None:
    later_cutoff = _card(training_cutoff=T0 + timedelta(days=30))  # an edited card file
    with pytest.raises(Refused, match="registered"):
        _evaluate(registry, card=later_cutoff)


def test_a_card_without_a_training_cutoff_cannot_be_scored_on_history(
    conn: sqlite3.Connection,
) -> None:
    card = _card(model_version="2", training_cutoff=None)
    reg = ArtifactRegistry(conn)
    reg.register("model", card.manifest(), actor="m07")
    with pytest.raises(Refused, match="cutoff"):
        _evaluate(reg, card=card)


def test_a_snapshot_at_or_before_the_training_cutoff_is_refused(
    registry: ArtifactRegistry,
) -> None:
    ev = _evaluate(registry)
    for as_of in (CUTOFF, CUTOFF - timedelta(hours=1)):
        with pytest.raises(Refused, match="cutoff"):
            predict_for_evaluation(ev, _snapshot(as_of), now=as_of + timedelta(seconds=5))
    after = CUTOFF + timedelta(minutes=5)
    assert predict_for_evaluation(ev, _snapshot(after), now=after + timedelta(seconds=5))


def test_the_evaluation_path_applies_the_label_rules_of_129(conn: sqlite3.Connection) -> None:
    """A card calibrated on another label is refused at load, and a labelled model's
    forecast carries its class triple, as on the live path."""
    stale = _card(model_version="4", label_version="m07.labels.tb.v0")
    labelled = _card(model_version="5", label_version=LABEL_VERSION)
    reg = ArtifactRegistry(conn)
    for card in (stale, labelled):
        reg.register("model", card.manifest(), actor="m07")
    with pytest.raises(Refused, match="another label"):
        _evaluate(reg, card=stale)
    p = predict_for_evaluation(_evaluate(reg, card=labelled), _snapshot(), now=NOW)
    assert p.class_probabilities is not None
    assert p.class_probabilities.label_version == LABEL_VERSION
    assert p.class_probabilities.up == p.probability.value


class _CutBaseline:
    """The baseline rule under a card with a training cutoff."""

    card = BASELINE.card.model_copy(update={"training_cutoff": CUTOFF})
    predict = BASELINE.predict


def test_the_live_predict_refuses_a_snapshot_at_or_before_the_cutoff() -> None:
    loaded = load_baseline(_CutBaseline(), mode=Mode.SHADOW)
    with pytest.raises(Refused, match="cutoff"):
        predict(loaded, _snapshot(CUTOFF), now=CUTOFF + timedelta(seconds=5))
    after = CUTOFF + timedelta(minutes=5)
    assert predict(loaded, _snapshot(after), now=after + timedelta(seconds=5))


def test_weights_naming_other_features_than_the_card_are_refused(conn: sqlite3.Connection) -> None:
    other = json.dumps({"features": [M15_DIR, H1_DIR], "intercept": ["0", "0", "0"],
                        "coef": [["1.5", "1.0"], ["-1.5", "-1.0"], ["0", "0"]],
                        "temperature": "1"}).encode()  # fmt: skip
    card = _card(other, model_version="3")
    reg = ArtifactRegistry(conn)
    reg.register("model", card.manifest(), actor="m07")
    with pytest.raises(Refused, match="features"):
        _evaluate(reg, card=card, weights=other)
