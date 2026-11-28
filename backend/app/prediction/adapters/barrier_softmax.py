"""A three-class softmax model on JSON weights (#127, #129): the tracer's trivial model.

It scores the triple-barrier label (`card.label_version`) as UP, DOWN and FLAT (FLAT
scored as NEUTRAL): `softmax((intercept + coef . x) / temperature)`, the temperature
fitted on a calibration slice later in time than the training data. One triple prices
both sides (M05 reads `up` for a long and `down` for a short).

Weights file: `{"features": [...], "intercept": [up, down, flat],
"coef": [[...] per class], "temperature": t}`. Numbers parse as Decimals, never floats;
the feature list must be the card's required features in order.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Direction
from app.prediction.card import ModelCard, verified_weights

CLASSES = (Direction.UP, Direction.DOWN, Direction.NEUTRAL)
"""The order of the weights' rows: UP, DOWN, then FLAT (scored as NEUTRAL)."""


@dataclass(frozen=True)
class BarrierSoftmax:
    card: ModelCard
    intercept: tuple[Decimal, ...]
    coef: tuple[tuple[Decimal, ...], ...]
    temperature: Decimal

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        x = [features[k] for k in self.card.required_features]
        rows = zip(self.intercept, self.coef, strict=True)
        z = [(b + sum((c * v for c, v in zip(row, x, strict=True)), Decimal(0)))
             / self.temperature for b, row in rows]  # fmt: skip
        top = max(z)
        e = [(v - top).exp() for v in z]
        return {k: v / sum(e) for k, v in zip(CLASSES, e, strict=True)}


def build(card: ModelCard, weights: bytes) -> BarrierSoftmax:
    w = json.loads(verified_weights(card, weights), parse_float=Decimal, parse_int=Decimal)
    n = len(card.required_features)
    if w["features"] != card.required_features:
        raise ValueError(f"{card.ref}: the weights' features are not the card's")
    if len(w["intercept"]) != len(CLASSES) or any(len(r) != n for r in w["coef"]):
        raise ValueError(f"{card.ref}: one intercept and one coefficient row per class")
    return BarrierSoftmax(card, tuple(map(Decimal, w["intercept"])),
                          tuple(tuple(map(Decimal, r)) for r in w["coef"]),
                          Decimal(w["temperature"]))  # fmt: skip
