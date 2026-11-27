"""Approval-gated loading and point-in-time prediction (#46; PRE-02, PRE-04, UST-03;
AT-16, AT-17).

Loading: a predictor with an artifact runs only if the M08 registry holds its
exact artifact hash and its newest eligibility record approves the requested
mode and has not expired. The artifact-free baseline needs no approval but never
runs Live.

Prediction: refused (with the reason) unless the snapshot is of the card's
schema, of usable quality, and known and unexpired at `now`; every required
feature is present; and every ranged feature is inside the range the model was
validated on (no extrapolation). News-enhanced models must be given the news
assessments they use, which are recorded as inputs; chart-only models never
are. The forecast expires at the earlier of the snapshot's expiry and the end
of the card's horizon. It describes market risk only.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from app.core.contracts import Confidence, FeatureSnapshot, PredictionGenerated
from app.core.enums import DataQuality, Direction, ForecastLineage, Mode
from app.core.ids import new_id
from app.core.time import TIMEFRAME_DURATION, live
from app.prediction.card import Predictor
from app.storage.registry import ArtifactRegistry

USABLE = (DataQuality.OK, DataQuality.DEGRADED)


class Refused(Exception):
    """The predictor may not run, or may not predict on these inputs."""


def load_predictor[P: Predictor](
    predictor: P, registry: ArtifactRegistry, *, mode: Mode, now: datetime
) -> P:
    card = predictor.card
    if card.artifact_hash is None:
        if mode is Mode.LIVE:
            raise Refused(f"{card.ref} is a baseline: never LIVE")
        return predictor
    manifest = registry.get("model", card.model_id, card.model_version)
    if manifest is None:
        raise Refused(f"{card.ref} is not registered")
    if manifest.artifact_hash != card.artifact_hash:
        raise Refused(f"{card.ref}: artifact hash differs from the registered one")
    record = registry.eligibility("model", card.ref)
    expired = record is not None and record.expires_at is not None and record.expires_at <= now
    if record is None or expired or mode not in record.approved_modes:
        raise Refused(f"{card.ref} is not approved for {mode}")
    return predictor


def _features(predictor: Predictor, snapshot: FeatureSnapshot) -> dict[str, Decimal]:
    card = predictor.card
    out: dict[str, Decimal] = {}
    for key in card.required_features:
        value = snapshot.values.get(key)
        if value is None:
            raise Refused(f"MISSING_FEATURE {key}")
        bound = card.feature_ranges.get(key)
        if bound is not None and not bound.low <= value <= bound.high:
            raise Refused(f"OUT_OF_DISTRIBUTION {key}={value}")
        out[key] = value
    return out


def predict(
    predictor: Predictor,
    snapshot: FeatureSnapshot,
    *,
    now: datetime,
    news_assessment_ids: Sequence[str] = (),
) -> PredictionGenerated:
    card = predictor.card
    if snapshot.feature_schema != card.feature_schema:
        raise Refused(f"snapshot schema {snapshot.feature_schema} is not {card.feature_schema}")
    if snapshot.quality not in USABLE or not live(snapshot.available_at, snapshot.valid_until, now):
        raise Refused(f"snapshot not usable at {now.isoformat()} ({snapshot.quality})")
    news = card.lineage is ForecastLineage.NEWS_ENHANCED
    if news and not news_assessment_ids:
        raise Refused(f"{card.ref} is news-enhanced but was given no news assessment")
    scores = predictor.predict(_features(predictor, snapshot))
    if not scores or any(not Decimal(0) <= s <= 1 for s in scores.values()):
        raise Refused(f"{card.ref} returned scores outside 0..1")
    top = max(scores.values())
    leaders = [d for d, s in scores.items() if s == top]
    direction = leaders[0] if len(leaders) == 1 else Direction.NEUTRAL
    horizon_end = now + card.horizon_bars * TIMEFRAME_DURATION[card.horizon]
    return PredictionGenerated(
        prediction_id=new_id("pred"),
        model_id=card.model_id,
        model_version=card.model_version,
        lineage=card.lineage,
        horizon=card.horizon,
        horizon_bars=card.horizon_bars,
        direction=direction,
        probability=Confidence(value=top, semantics=card.semantics, calibrated=card.calibrated),
        data_quality=snapshot.quality,
        input_refs=[
            f"{snapshot.feature_schema}@{snapshot.as_of.isoformat()}",
            *(news_assessment_ids if news else ()),
        ],
        generated_at=now,
        valid_until=min(snapshot.valid_until, horizon_end),
    )
