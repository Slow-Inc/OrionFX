"""Adaptive ensemble (#47; PRE-03, PRE-04; AT-17).

Combines member forecasts of one lineage and one horizon into one forecast:

- Each member comes with the card of the exact model that made it (id, version,
  lineage, horizon); a mismatched pair is a wiring error and refused.
- Eligible members are of the requested lineage and horizon, live at `now`, of
  usable data quality, and better than the baseline on validation: weight =
  the card's skill, halved when the member's data is DEGRADED [PROPOSED].
- Repeats of one model on the same inputs: only the most recent counts.
- Correlated inputs are not double counted: members reading the same inputs
  form one vote whose weight is its heaviest member's (not the sum), and whose
  direction comes from the members' weighted scores (a tie abstains).
- The winning direction has the largest weighted score across votes; a tie
  gives no forecast. The score is that direction's share of the total weight,
  labelled uncalibrated with its horizon. It expires with its earliest member.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.contracts import Confidence, PredictionGenerated
from app.core.enums import DataQuality, Direction, ForecastLineage, Timeframe
from app.core.ids import new_id
from app.core.time import live
from app.market_data.features import USABLE_QUALITY
from app.prediction.card import ModelCard, unique_leader

ENSEMBLE_ID = "ensemble"
ENSEMBLE_VERSION = "1.0.0"
DEGRADED_FACTOR = Decimal("0.5")  # [PROPOSED]


class WiringError(ValueError):
    """A prediction was paired with a card that does not describe its model."""


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    prediction: PredictionGenerated | None
    reason: str = ""
    weights: dict[str, Decimal] = field(default_factory=dict)


def _check_pair(pred: PredictionGenerated, card: ModelCard) -> None:
    said = (pred.model_id, pred.model_version, pred.lineage, pred.horizon, pred.horizon_bars)
    card_says = (card.model_id, card.model_version, card.lineage, card.horizon, card.horizon_bars)
    if said != card_says:
        raise WiringError(f"{pred.prediction_id} paired with the card of {card.ref}")


def ensemble(
    members: Sequence[tuple[PredictionGenerated, ModelCard]],
    *,
    now: datetime,
    lineage: ForecastLineage,
    horizon: Timeframe,
    horizon_bars: int,
) -> EnsembleResult:
    latest: dict[tuple[str, frozenset[str]], tuple[PredictionGenerated, Decimal]] = {}
    for pred, card in members:
        _check_pair(pred, card)
        if (pred.lineage, pred.horizon, pred.horizon_bars) != (lineage, horizon, horizon_bars):
            continue
        if not live(pred.generated_at, pred.valid_until, now):
            continue
        if pred.data_quality not in USABLE_QUALITY:
            continue
        weight = card.skill * (
            DEGRADED_FACTOR if pred.data_quality is DataQuality.DEGRADED else Decimal(1)
        )
        if weight <= 0:
            continue
        key = (pred.model_id, frozenset(pred.input_refs))
        seen = latest.get(key)
        if seen is None or (pred.generated_at, pred.prediction_id) > (
            seen[0].generated_at,
            seen[0].prediction_id,
        ):
            latest[key] = (pred, weight)
    chosen = list(latest.values())
    if not chosen:
        return EnsembleResult(None, "no eligible member")
    weights = {p.prediction_id: w for p, w in chosen}

    votes: dict[frozenset[str], list[tuple[PredictionGenerated, Decimal]]] = {}
    for p, w in chosen:
        votes.setdefault(frozenset(p.input_refs), []).append((p, w))
    tally: dict[Direction, Decimal] = {}
    total = Decimal(0)
    for group in votes.values():
        group_weight = max(w for _, w in group)
        mass = sum((w for _, w in group), Decimal(0))
        scores: dict[Direction, Decimal] = {}
        for p, w in group:
            scores[p.direction] = scores.get(p.direction, Decimal(0)) + w * p.probability.value
        leader = unique_leader({d: s / mass for d, s in scores.items()})
        total += group_weight
        if leader is not None:
            direction, score = leader
            tally[direction] = tally.get(direction, Decimal(0)) + group_weight * score
    winner = unique_leader(tally)
    if winner is None:
        return EnsembleResult(None, "weighted tie between directions", weights)
    direction, score = winner
    degraded = any(p.data_quality is DataQuality.DEGRADED for p, _ in chosen)
    return EnsembleResult(
        PredictionGenerated(
            prediction_id=new_id("ens"),
            model_id=ENSEMBLE_ID,
            model_version=ENSEMBLE_VERSION,
            lineage=lineage,
            horizon=horizon,
            horizon_bars=horizon_bars,
            direction=direction,
            probability=Confidence(
                value=score / total,
                semantics=(
                    f"weighted agreement of member scores over {horizon_bars} {horizon} bars "
                    "(weights = validation skill over the baseline); uncalibrated"
                ),
            ),
            data_quality=DataQuality.DEGRADED if degraded else DataQuality.OK,
            input_refs=[p.prediction_id for p, _ in chosen],
            generated_at=now,
            valid_until=min(p.valid_until for p, _ in chosen),
        ),
        weights=weights,
    )
