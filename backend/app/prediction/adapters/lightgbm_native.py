"""A LightGBM multiclass model on native text boosters (#130): the M04 baseline family.

It scores the triple-barrier label (`card.label_version`) as UP, DOWN and FLAT (FLAT
scored as NEUTRAL) from the M04 transforms (`card.feature_schema`). One booster per seed;
the served triple is `calibrate`: the mean of the seeds' softmax probabilities, then
temperature-scaled (softmax(log p_mean / T)). The trainer fits T through this same
function, so calibration and serving cannot drift apart.

Weights file (`FORMAT`, canonical JSON, pinned by `card.weights_hash`):
`{"format", "classes": ["UP", "DOWN", "FLAT"], "features": [...], "feature_schema",
"label_version", "training_cutoff", "training": {...}, "config": {...}, "temperature",
"boosters": [{"seed", "sha256", "model"}]}`. Each `model` is the text LightGBM writes
with `model_to_string`; its sha256 is checked before LightGBM parses it, and it is parsed
only by `lightgbm.Booster(model_str=...)`. Nothing here unpickles. Every field must agree
with the card, which the registry pins: a schema, label, feature list, seed list or config
that differs is refused. Floats stay inside; the port speaks Decimal.
"""

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray

from app.core.bet import LABEL_VERSION
from app.core.contracts import canonical_json, sha256_ref
from app.core.enums import Direction
from app.prediction.card import ModelCard, verified_weights
from app.prediction.transforms import TRANSFORM_SCHEMA

FORMAT = "m04.lightgbm.v1"
CLASSES = ("UP", "DOWN", "FLAT")
"""The boosters' class order (label 0, 1, 2), scored as UP, DOWN and NEUTRAL."""
DIRECTIONS = (Direction.UP, Direction.DOWN, Direction.NEUTRAL)
_FLOOR = 1e-15


def calibrate(seed_probs: NDArray[np.float64], temperature: float) -> NDArray[np.float64]:
    """(seeds, rows, 3) softmax outputs -> (rows, 3): the seed mean, temperature-scaled."""
    mean = seed_probs.mean(axis=0)
    z = np.log(np.clip(mean, _FLOOR, 1.0)) / temperature
    e = np.exp(z - z.max(axis=1, keepdims=True))
    out: NDArray[np.float64] = e / e.sum(axis=1, keepdims=True)
    return out


def seed_probs(boosters: Sequence[lgb.Booster], x: NDArray[np.float64]) -> NDArray[np.float64]:
    """(seeds, rows, 3): each booster's softmax outputs."""
    return np.stack([np.asarray(b.predict(x)) for b in boosters])


def config_hash(config: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of a training config."""
    return sha256_ref(canonical_json(config))


@dataclass(frozen=True)
class LightGBMNative:
    card: ModelCard
    boosters: tuple[lgb.Booster, ...]
    temperature: float

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        x = np.array([[float(features[k]) for k in self.card.required_features]])
        p = calibrate(seed_probs(self.boosters, x), self.temperature)[0]
        return {d: Decimal(float(v)) for d, v in zip(DIRECTIONS, p, strict=True)}


def _require(ok: bool, card: ModelCard, problem: str) -> None:
    if not ok:
        raise ValueError(f"{card.ref}: {problem}")


def build(card: ModelCard, weights: bytes) -> LightGBMNative:
    w = json.loads(verified_weights(card, weights))
    _require(w["format"] == FORMAT, card, f"the weights are not {FORMAT}")
    _require(tuple(w["classes"]) == CLASSES, card, "the class order is not UP, DOWN, FLAT")
    _require(w["features"] == card.required_features, card,
             "the weights' features are not the card's")  # fmt: skip
    _require(w["feature_schema"] == card.feature_schema == TRANSFORM_SCHEMA, card,
             f"the feature schema is not {TRANSFORM_SCHEMA} on both card and weights")  # fmt: skip
    _require(w["label_version"] == card.label_version == LABEL_VERSION, card,
             f"the label is not {LABEL_VERSION} on both card and weights")  # fmt: skip
    cutoff = card.training_cutoff
    _require(cutoff is not None and w["training_cutoff"] == cutoff.isoformat(), card,
             "the training cutoff is not the card's")  # fmt: skip
    training = card.training
    if training is None or w["training"] != training.model_dump(mode="json"):
        raise ValueError(f"{card.ref}: the training record is not the card's")
    _require(config_hash(w["config"]) == training.config_hash, card,
             "the config is not the one its hash names")  # fmt: skip
    _require([b["seed"] for b in w["boosters"]] == training.seeds, card,
             "one booster per seed of the card, in order")  # fmt: skip
    temperature = float(Decimal(w["temperature"]))
    _require(math.isfinite(temperature) and temperature > 0, card, "a bad temperature")
    boosters = []
    for b in w["boosters"]:
        text = b["model"]
        _require(sha256_ref(text.encode()) == b["sha256"], card,
                 f"booster seed {b['seed']} is not the one its hash names")  # fmt: skip
        booster = lgb.Booster(model_str=text)
        shape = (booster.num_feature(), booster.num_model_per_iteration())
        _require(
            shape == (len(card.required_features), len(CLASSES)),
            card,
            f"booster seed {b['seed']} does not score 3 classes of the card's features",
        )
        boosters.append(booster)
    return LightGBMNative(card, tuple(boosters), temperature)
