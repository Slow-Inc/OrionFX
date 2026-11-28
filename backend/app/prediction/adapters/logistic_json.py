"""A logistic model on JSON weights (#127, the training-loop tracer's trivial model).

`P(UP) = sigmoid(a * z + b)` with `z = intercept + sum(coef_i * x_i)`: a logistic
score with Platt calibration. The weights file is
`{"features": [...], "coef": [...], "intercept": ..., "platt": {"a": ..., "b": ...}}`;
numbers are read as Decimals, never as floats, and the feature list must be the card's
required features in order. The score for DOWN is `1 - P(UP)`.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Direction
from app.prediction.card import ModelCard, verified_weights


@dataclass(frozen=True)
class Logistic:
    card: ModelCard
    coef: tuple[Decimal, ...]
    intercept: Decimal
    a: Decimal
    b: Decimal

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        z = self.intercept + sum(
            (c * features[k] for c, k in zip(self.coef, self.card.required_features, strict=True)),
            Decimal(0),
        )
        up = 1 / (1 + (-(self.a * z + self.b)).exp())
        return {Direction.UP: up, Direction.DOWN: 1 - up}


def build(card: ModelCard, weights: bytes) -> Logistic:
    w = json.loads(verified_weights(card, weights), parse_float=Decimal, parse_int=Decimal)
    if w["features"] != card.required_features or len(w["coef"]) != len(w["features"]):
        raise ValueError(f"{card.ref}: the weights' features are not the card's")
    platt = w["platt"]
    return Logistic(card, tuple(map(Decimal, w["coef"])), Decimal(w["intercept"]),
                    Decimal(platt["a"]), Decimal(platt["b"]))  # fmt: skip
