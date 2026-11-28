"""Synthetic export rows for the M04 trainer's unit tests (#130). A support module.

Rows look like `export_dataset(..., transforms=True)` output: one per M5 bar, split by
time into train, validation and holdout, with two M04 transform features and an H1
triple-barrier label. `H1.close_sma_atr` drives P(UP) - P(DOWN); `M15.close_sma_atr` is
noise. Labels resolve 5-60 minutes after the bar, so they overlap. Fixture data only.
"""

import functools
import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.bet import LABEL_VERSION
from app.core.enums import Timeframe
from app.learning.trainer import LightGBMConfig, TrainedModel, train
from app.prediction.transforms import TRANSFORM_SCHEMA

T0 = datetime(2026, 2, 2, tzinfo=UTC)
DATASET_REF = "synthetic@1"
FEATURES = ["H1.close_sma_atr", "M15.close_sma_atr"]
SPLITS = (("train", 3000), ("validation", 1000), ("holdout", 1000))
TINY = LightGBMConfig(features=FEATURES, horizon=Timeframe.H1, horizon_bars=1, seeds=[1, 2, 3],
                      num_leaves=4, max_depth=3, min_data_in_leaf=20, learning_rate=0.1,
                      num_boost_round=200, early_stopping_rounds=10, lambda_l2=1.0)  # fmt: skip
"""Small and fast; bagging and feature_fraction below 1 make the seeds' boosters differ."""


def rows(seed: int = 130) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    i = 0
    for split, n in SPLITS:
        for _ in range(n):
            as_of = T0 + timedelta(minutes=5 * i)
            i += 1
            x1, x2 = rng.gauss(0, 1), rng.gauss(0, 1)
            up = 1 / (1 + math.exp(-1.5 * x1))
            flat = 0.25
            u = rng.random()
            outcome = "FLAT" if u < flat else ("UP" if u < flat + (1 - flat) * up else "DOWN")
            out.append({
                "split": split, "as_of": as_of.isoformat(), "feature_schema": TRANSFORM_SCHEMA,
                "features": {FEATURES[0]: f"{x1:.6f}", FEATURES[1]: f"{x2:.6f}"},
                "barrier_labels": {"H1x1": {
                    "label_version": LABEL_VERSION, "outcome": outcome,
                    "available_at": (as_of + timedelta(minutes=rng.choice(range(5, 65, 5)),
                                                       seconds=1)).isoformat()}},
            })  # fmt: skip
    return out


@functools.cache
def trained() -> TrainedModel:
    """`TINY` trained on `rows()`, once per test session."""
    return train(rows(), TINY, dataset_ref=DATASET_REF, code_commit="test")
