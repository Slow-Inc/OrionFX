"""The labelled baseline predictor (#46; M04 acceptance "comparison against baseline,
no invented accuracy claims").

A fixed rule, not a model: the forecast direction is the H1 multi-bar trend
direction from the M02 snapshot, with a constant score of 0.5 that is stated to
be no probability at all. It lets the pipeline run, and gives trained models
something to beat, before any model exists. Shadow/Demo only.
"""

from collections.abc import Mapping
from decimal import Decimal

from app.core.enums import Direction, ForecastLineage, Timeframe
from app.market_data.features import FEATURE_SCHEMA
from app.prediction.card import ModelCard

TREND_KEY = "H1.direction_10.direction"
BASELINE_SCORE = Decimal("0.5")  # [PROPOSED] deliberately uninformative


class Baseline:
    card = ModelCard(
        model_id="baseline-h1-trend",
        model_version="1.0.0",
        artifact_hash=None,
        lineage=ForecastLineage.CHART_ONLY,
        feature_schema=FEATURE_SCHEMA,
        required_features=[TREND_KEY],
        feature_ranges={},
        horizon=Timeframe.H1,
        horizon_bars=1,
        semantics="rule-based baseline (H1 trend persists); uncalibrated; not a probability",
        calibrated=False,
        validation={},
        evidence_refs=[],
    )

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        trend = features[TREND_KEY]
        direction = (
            Direction.UP if trend > 0 else Direction.DOWN if trend < 0 else Direction.NEUTRAL
        )
        return {direction: BASELINE_SCORE}


BASELINE = Baseline()
