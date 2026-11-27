"""Adaptive ensemble (#47; PRE-03, PRE-04; AT-17).

Combines member forecasts of one lineage and one horizon into one forecast:

- Eligible members are live at `now` with usable data quality and a card that
  beats the baseline on validation: weight = baseline Brier - model Brier
  (skill), halved when the member's data is DEGRADED [PROPOSED].
- Correlated members (same model, identical inputs) count once: the heaviest.
- Lineages never mix: chart-only and news-enhanced are combined separately.
- The winning direction has the largest weighted score; a tie gives no forecast.
  The score is that direction's weighted score as a share of the total weight,
  stated as uncalibrated. The forecast expires with its earliest member.

No eligible member, mismatched horizons or a tie give no forecast and a reason.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.contracts import Confidence, PredictionGenerated
from app.core.enums import DataQuality, Direction, ForecastLineage
from app.core.ids import new_id
from app.core.time import live
from app.market_data.features import USABLE_QUALITY
from app.prediction.card import ModelCard

ENSEMBLE_ID = "ensemble"
ENSEMBLE_VERSION = "1.0.0"
DEGRADED_FACTOR = Decimal("0.5")  # [PROPOSED]
SEMANTICS = (
    "weighted agreement of eligible member scores (weights = validation skill over "
    "the baseline); uncalibrated"
)


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    prediction: PredictionGenerated | None
    reason: str = ""
    weights: dict[str, Decimal] = field(default_factory=dict)


def _skill(card: ModelCard) -> Decimal:
    v = card.validation
    if "brier" not in v or "baseline_brier" not in v:
        return Decimal(0)
    return max(v["baseline_brier"] - v["brier"], Decimal(0))


def ensemble(
    members: Sequence[tuple[PredictionGenerated, ModelCard]],
    *,
    now: datetime,
    lineage: ForecastLineage,
) -> EnsembleResult:
    best: dict[tuple[str, tuple[str, ...]], tuple[PredictionGenerated, Decimal]] = {}
    for pred, card in members:
        if pred.lineage is not lineage or card.model_id != pred.model_id:
            continue
        if not live(pred.generated_at, pred.valid_until, now):
            continue
        if pred.data_quality not in USABLE_QUALITY:
            continue
        weight = _skill(card)
        if pred.data_quality is DataQuality.DEGRADED:
            weight *= DEGRADED_FACTOR
        if weight <= 0:
            continue
        key = (pred.model_id, tuple(sorted(pred.input_refs)))
        if key not in best or weight > best[key][1]:
            best[key] = (pred, weight)
    chosen = list(best.values())
    if not chosen:
        return EnsembleResult(None, "no eligible member")
    horizons = {(p.horizon, p.horizon_bars) for p, _ in chosen}
    if len(horizons) > 1:
        return EnsembleResult(None, f"members disagree on the horizon: {sorted(horizons)}")
    weights = {p.prediction_id: w for p, w in chosen}
    total = sum(weights.values(), Decimal(0))
    by_dir: dict[Direction, Decimal] = {}
    for p, w in chosen:
        by_dir[p.direction] = by_dir.get(p.direction, Decimal(0)) + w * p.probability.value
    top = max(by_dir.values())
    leaders = [d for d, s in by_dir.items() if s == top]
    if len(leaders) > 1:
        return EnsembleResult(None, "weighted tie between directions", weights)
    first = chosen[0][0]
    degraded = any(p.data_quality is DataQuality.DEGRADED for p, _ in chosen)
    quality = DataQuality.DEGRADED if degraded else DataQuality.OK
    return EnsembleResult(
        PredictionGenerated(
            prediction_id=new_id("ens"),
            model_id=ENSEMBLE_ID,
            model_version=ENSEMBLE_VERSION,
            lineage=lineage,
            horizon=first.horizon,
            horizon_bars=first.horizon_bars,
            direction=leaders[0],
            probability=Confidence(value=top / total, semantics=SEMANTICS),
            data_quality=quality,
            input_refs=[p.prediction_id for p, _ in chosen],
            generated_at=now,
            valid_until=min(p.valid_until for p, _ in chosen),
        ),
        weights=weights,
    )
