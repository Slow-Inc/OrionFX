"""The versioned `feature.snapshot` (#31; DAT-02, DAT-04, UST-03).

Built point-in-time: only bars whose `available_at <= now` are read, so a
snapshot for time T never changes when later bars arrive. Every technique of the
MVP library runs per timeframe; a technique that cannot produce values (warm-up,
gap, stale, missing input) contributes its key with None. Quality is the worst
input: STALE if any input expired, DEGRADED if any value is missing, else OK.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from app.core.contracts import CandleClosed, FeatureSnapshot
from app.core.enums import DataQuality, QualityFlag, Timeframe
from app.market_data.techniques.indicators import Technique, make_library, observe
from app.market_data.techniques.price_action import PRICE_ACTION, multi_timeframe

FEATURE_SCHEMA = "m02.features.v1"


def default_techniques() -> list[Technique]:
    return [*make_library().values(), *PRICE_ACTION.values()]


def build_feature_snapshot(
    symbol: str,
    bars_by_tf: Mapping[Timeframe, Sequence[CandleClosed]],
    now: datetime,
    techniques: Sequence[Technique] | None = None,
) -> FeatureSnapshot:
    known = {tf: [b for b in bars if b.available_at <= now] for tf, bars in bars_by_tf.items()}
    if not any(known.values()):
        raise ValueError("no bar was available at `now`")
    values: dict[str, Decimal | None] = {}
    stale = missing = False
    valid_until: list[datetime] = []
    for tf, bars in known.items():
        if not bars:
            missing = True
            continue
        for tech in techniques or default_techniques():
            obs = observe(tech, bars, now)
            if QualityFlag.STALE in obs.quality_flags:
                stale = True
            if not obs.values:
                missing = True
                values[f"{tf}.{tech.technique_id}"] = None
                continue
            valid_until.append(obs.valid_until)
            for key, value in obs.values.items():
                values[f"{tf}.{tech.technique_id}.{key}"] = value
                missing = missing or value is None
    if len(known) > 1:
        for key, value in multi_timeframe(known, now).values.items():
            values[f"mtf.{key}"] = value
            missing = missing or value is None
    quality = DataQuality.STALE if stale else DataQuality.DEGRADED if missing else DataQuality.OK
    return FeatureSnapshot(
        symbol=symbol,
        feature_schema=FEATURE_SCHEMA,
        values=values,
        quality=quality,
        as_of=max(bars[-1].close_time for bars in known.values() if bars),
        available_at=now,
        valid_until=max([now, *([min(valid_until)] if valid_until else [])]),
    )
