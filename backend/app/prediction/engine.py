"""Approval-gated loading and point-in-time prediction (#46; PRE-02, PRE-04, UST-03;
AT-16, AT-17).

Loading: a model is code plus weights in an allowlisted package, loaded only by
the M08 `ArtifactLoader`, which hashes the bytes actually executed against the
registry and requires a current eligibility record for the mode. The engine then
checks that the module built the card of the model it was loaded as, stamps the
registry-verified hash onto that card (a file cannot contain its own hash), and
requires the approval not to have expired at the caller's `now` (point-in-time,
for replay). The built-in baseline needs no
artifact but never runs Live. Only a `LoadedPredictor` can predict: the gate
cannot be skipped by handing `predict` a bare object.

Prediction is refused, with the reason, unless the snapshot is usable at `now`
(M02's rule), every required feature is present and inside the range the model
was validated on, and the model's scores are finite Decimals in 0..1 with one
clear leader. The score reported is the model's score for the direction it
predicts, and the semantics say so. News-enhanced inference is refused until
news features exist (#48); passing news to a chart-only model is a wiring error
and refused too. The forecast covers `horizon_bars` from the snapshot's bar and
expires no later than the snapshot. It describes market risk only.

Evaluation (#127): scoring history needs a model registered today to run at past
`now`s, which no approval covers. `load_for_evaluation` therefore reads no eligibility:
the card must be the registered one, the adapter's bytes are hash-verified, the adapter
checks the weights against the card, and the card must state a training cutoff. It
returns an `EvaluationPredictor`, a distinct type that only `predict_for_evaluation`
accepts; `predict`, `MarketRecorder` and `DecisionLoop` refuse it. Every prediction
refuses a snapshot at or before the card's training cutoff (in-sample).

A calibrated card that names its `label_version` and scores UP, DOWN and NEUTRAL (FLAT)
gets those three scores on the forecast as class probabilities (#129), which M05 prices
EV from; three scores that do not sum to 1 are refused, never renormalised.
"""

import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError

from app.core.bet import LABEL_VERSION
from app.core.contracts import (
    ClassProbabilities,
    Confidence,
    FeatureSnapshot,
    PredictionGenerated,
)
from app.core.enums import DataQuality, Direction, ForecastLineage, Mode
from app.core.ids import new_id
from app.core.time import TIMEFRAME_DURATION
from app.market_data.features import snapshot_problem
from app.prediction.card import ModelCard, Predictor, unique_leader, verified_weights
from app.storage.artifacts import ArtifactLoader, UntrustedArtifact
from app.storage.registry import ArtifactRegistry, subject_ref

_GATE = object()


class Refused(Exception):
    """The predictor may not run, or may not predict on these inputs."""


@dataclass(frozen=True, slots=True)
class LoadedPredictor:
    """A predictor that passed the gate; the only thing `predict` accepts."""

    predictor: Predictor
    mode: Mode
    _gate: object
    approved_until: datetime | None = None
    """When the approval it was loaded under expires; `predict` refuses from then on."""

    def __post_init__(self) -> None:
        if self._gate is not _GATE:
            raise Refused("predictors are loaded through load_model or load_baseline")

    @property
    def card(self) -> ModelCard:
        return self.predictor.card


@dataclass(frozen=True, slots=True)
class _Stamped:
    """A loaded model whose card carries the registry-verified artifact hash."""

    inner: Predictor
    card: ModelCard

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        return self.inner.predict(features)


def load_baseline(predictor: Predictor, *, mode: Mode) -> LoadedPredictor:
    if predictor.card.artifact_hash is not None:
        raise Refused(f"{predictor.card.ref} has an artifact: load it with load_model")
    if mode is Mode.LIVE:
        raise Refused(f"{predictor.card.ref} is a baseline: never LIVE")
    _check_label(predictor.card)
    return LoadedPredictor(predictor, mode, _GATE)


def load_model(
    loader: ArtifactLoader,
    registry: ArtifactRegistry,
    *,
    model_id: str,
    version: str,
    module: str,
    mode: Mode,
    now: datetime,
    card: ModelCard | None = None,
    weights: bytes | None = None,
) -> LoadedPredictor:
    """The module must define `build() -> Predictor`; its bytes are hash-verified. For a
    trained model (#130), `module` is a reviewed adapter defining `build(card, weights)`:
    pass the registered `card` and its `weights`, checked as `load_for_evaluation` checks
    them, and the approval is still required."""
    adapter: tuple[ModelCard, bytes] | None = None
    if card is not None or weights is not None:
        if card is None or weights is None or card.ref != subject_ref(model_id, version):
            raise Refused(f"{model_id}@{version}: an adapter model loads with its own card"
                          " and its weights")  # fmt: skip
        _check_registered(registry, card)
        adapter = (card, weights)
    try:
        code: types.ModuleType = loader.load("model", model_id, version, module, mode=mode,
                                             at=now)  # fmt: skip
    except UntrustedArtifact as exc:
        raise Refused(str(exc)) from None
    built = code.build() if adapter is None else _build(code, *adapter, module)
    manifest = registry.get("model", model_id, version)
    if manifest is None or (built.card.model_id, built.card.model_version) != (model_id, version):
        raise Refused(f"{module} built a card for another model")
    _check_label(built.card)
    # A file cannot name its own hash: the verified hash comes from the registry.
    predictor = _Stamped(
        built, built.card.model_copy(update={"artifact_hash": manifest.artifact_hash})
    )
    # Point-in-time (#126, MAJ-09.1): the record that was current at `now`, not the newest.
    record = registry.eligibility_at("model", predictor.card.ref, now)
    if record is None or not record.approves(mode, now):
        raise Refused(f"{predictor.card.ref} is not approved for {mode} at {now.isoformat()}")
    return LoadedPredictor(predictor, mode, _GATE, record.expires_at)


@dataclass(frozen=True, slots=True)
class EvaluationPredictor:
    """A hash-verified model for scoring history, with no approval behind it."""

    predictor: Predictor
    _gate: object

    def __post_init__(self) -> None:
        if self._gate is not _GATE:
            raise Refused("evaluation predictors are loaded through load_for_evaluation")


def load_for_evaluation(
    loader: ArtifactLoader,
    registry: ArtifactRegistry,
    *,
    card: ModelCard,
    weights: bytes,
    module: str,
) -> EvaluationPredictor:
    """`module` is a reviewed adapter defining `build(card, weights) -> Predictor`; it gets
    weights already checked against the card, and checks them again before parsing."""
    if card.training_cutoff is None:
        raise Refused(f"{card.ref} states no training cutoff: history cannot be scored")
    _check_registered(registry, card)
    try:
        code = loader.load_for_research("model", card.model_id, card.model_version, module)
    except UntrustedArtifact as exc:
        raise Refused(str(exc)) from None
    return EvaluationPredictor(_build(code, card, weights, module), _GATE)


def _check_registered(registry: ArtifactRegistry, card: ModelCard) -> None:
    _check_label(card)
    manifest = registry.get("model", card.model_id, card.model_version)
    if card.artifact_hash is None or manifest != card.manifest():
        raise Refused(f"{card.ref}: this card is not the registered one")


def _build(code: types.ModuleType, card: ModelCard, weights: bytes, module: str) -> Predictor:
    """An adapter's model: the weights are checked against the card before the adapter
    sees them (and the adapter checks again before parsing)."""
    try:
        built: Predictor = code.build(card, verified_weights(card, weights))
    except Exception as exc:
        raise Refused(f"{module} refused {card.ref}: {exc}") from None
    if built.card != card:
        raise Refused(f"{module} built another card than {card.ref}")
    return built


def _features(card: ModelCard, values: Mapping[str, Decimal | None]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for key in card.required_features:
        value = values.get(key)
        if value is None:
            raise Refused(f"MISSING_FEATURE {key}")
        bound = card.feature_ranges.get(key)
        if bound is not None and not bound.low <= value <= bound.high:
            raise Refused(f"OUT_OF_DISTRIBUTION {key}={value}")
        out[key] = value
    return out


def _leader(card: ModelCard, scores: Mapping[Direction, Decimal]) -> tuple[Direction, Decimal]:
    if not scores or not all(
        isinstance(s, Decimal) and s.is_finite() and 0 <= s <= 1 for s in scores.values()
    ):
        raise Refused(f"{card.ref} returned scores that are not finite Decimals in 0..1")
    leader = unique_leader(scores)
    if leader is None:
        raise Refused(f"{card.ref} gave no signal: tied scores")
    return leader


def _classes(card: ModelCard, scores: Mapping[Direction, Decimal]) -> ClassProbabilities | None:
    """The class probabilities of a calibrated three-class model with a label version
    (#129); None for any other model. One triple prices both sides."""
    if not card.calibrated or card.label_version is None:
        return None
    if set(scores) != set(Direction):
        raise Refused(f"{card.ref}: a labelled model must score UP, DOWN and NEUTRAL")
    try:
        return ClassProbabilities(
            label_version=card.label_version,
            up=scores[Direction.UP],
            down=scores[Direction.DOWN],
            flat=scores[Direction.NEUTRAL],
        )
    except ValidationError:
        raise Refused(f"{card.ref}: its class probabilities do not sum to 1") from None


def _check_label(card: ModelCard) -> None:
    """A model calibrated on another label would never be priced: refuse it at load."""
    if card.label_version is not None and card.label_version != LABEL_VERSION:
        raise Refused(
            f"{card.ref} is calibrated on another label ({card.label_version}), not {LABEL_VERSION}"
        )


def predict(
    loaded: LoadedPredictor,
    snapshot: FeatureSnapshot,
    *,
    now: datetime,
    news_assessment_ids: Sequence[str] = (),
) -> PredictionGenerated:
    if not isinstance(loaded, LoadedPredictor):
        raise Refused("predict accepts only a loaded predictor")
    if loaded.approved_until is not None and now >= loaded.approved_until:
        raise Refused(f"{loaded.card.ref}: its approval expired at "
                      f"{loaded.approved_until.isoformat()}")  # fmt: skip
    return _forecast(loaded.predictor, snapshot, now, news_assessment_ids)


def predict_for_evaluation(
    model: EvaluationPredictor, snapshot: FeatureSnapshot, *, now: datetime
) -> PredictionGenerated:
    """`predict` for a model loaded by `load_for_evaluation`: history only."""
    if not isinstance(model, EvaluationPredictor):
        raise Refused("predict_for_evaluation accepts only an evaluation predictor")
    return _forecast(model.predictor, snapshot, now, ())


def _forecast(
    predictor: Predictor,
    snapshot: FeatureSnapshot,
    now: datetime,
    news_assessment_ids: Sequence[str],
) -> PredictionGenerated:
    card = predictor.card
    if card.training_cutoff is not None and snapshot.as_of <= card.training_cutoff:
        raise Refused(f"{card.ref}: {snapshot.ref} is not after its training cutoff "
                      f"{card.training_cutoff.isoformat()} (in-sample)")  # fmt: skip
    if card.lineage is ForecastLineage.NEWS_ENHANCED:
        raise Refused(f"{card.ref}: news-enhanced inference needs news features (not built, #48)")
    if news_assessment_ids:
        raise Refused(f"{card.ref} is chart-only: news inputs would be silently ignored")
    problem = snapshot_problem(snapshot, now, schema=card.feature_schema)
    if problem is not None:
        raise Refused(problem)
    try:
        scores = predictor.predict(_features(card, snapshot.values))
    except Refused:
        raise
    except Exception as exc:
        raise Refused(f"{card.ref} failed: {type(exc).__name__}") from None
    direction, score = _leader(card, scores)
    classes = _classes(card, scores)
    horizon_end = snapshot.as_of + card.horizon_bars * TIMEFRAME_DURATION[card.horizon]
    if horizon_end <= now:
        raise Refused(f"{card.ref}: the forecast horizon ended at {horizon_end.isoformat()}")
    return PredictionGenerated(
        prediction_id=new_id("pred"),
        model_id=card.model_id,
        model_version=card.model_version,
        lineage=card.lineage,
        horizon=card.horizon,
        horizon_bars=card.horizon_bars,
        direction=direction,
        probability=Confidence(
            value=score,
            semantics=f"{card.semantics}; the model's score for the predicted direction",
            calibrated=card.calibrated,
        ),
        # Judged on what the model read (#124, BLK-01b): the snapshot is usable and live,
        # and every required feature is present and in range, or we refused above.
        data_quality=DataQuality.OK,
        input_refs=[snapshot.ref],
        generated_at=now,
        valid_until=min(snapshot.valid_until, horizon_end),
        class_probabilities=classes,
    )
