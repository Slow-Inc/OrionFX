"""The versioned `feature.snapshot` (#31; DAT-02, DAT-04, UST-03).

- Point-in-time: only bars with `available_at <= now` are read, so a snapshot
  for time T never changes when later bars arrive.
- Fixed shape: every technique reports all of its declared outputs for every
  requested timeframe, as `{tf}.{technique_id}.{key}`; unusable ones are None.
  `mtf.aligned` is 1 when every timeframe's direction agrees (and is not flat),
  0 when they disagree, None when any direction is unknown (CAT-T03-07).
- Quality is the worst input: MISSING if a requested timeframe has no bar,
  STALE if any input expired, DEGRADED if a technique could not run (warm-up,
  gap, missing input), else OK. A technique answering "no level here" (None)
  does not degrade the snapshot: that is information, not missing data.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from app.core.contracts import CandleClosed, FeatureSnapshot
from app.core.enums import DataQuality, QualityFlag, Timeframe
from app.core.time import live
from app.market_data.techniques.indicators import Technique, make_library, observe
from app.market_data.techniques.price_action import make_price_action

FEATURE_SCHEMA = "m02.features.v1"
DEFAULT_TECHNIQUES: tuple[Technique, ...] = (
    *make_library().values(),
    *make_price_action().values(),
)


def _key(tf: Timeframe, technique_id: str, output: str) -> str:
    """The snapshot key format: the contract between M02 and every consumer."""
    return f"{tf}.{technique_id}.{output}"


def feature_key(tf: Timeframe, catalog_id: str, output: str) -> str:
    """The snapshot key of one output of the default technique for a catalog row, so
    consumers name features by catalog id, not by technique-id spelling."""
    for t in DEFAULT_TECHNIQUES:
        if t.catalog_id == catalog_id and output in t.outputs:
            return _key(tf, t.technique_id, output)
    raise KeyError(f"no default technique for {catalog_id} with output {output!r}")


SUMMARY_CATALOG_IDS = ("CAT-T03-04", "CAT-T03-06", "CAT-T03-07")
"""Structure, regime and multi-bar direction: the chart summary other modules may read."""
SUMMARY_KEYS: frozenset[str] = frozenset(
    {
        _key(tf, t.technique_id, key)
        for tf in Timeframe
        for t in DEFAULT_TECHNIQUES
        if t.catalog_id in SUMMARY_CATALOG_IDS
        for key in t.outputs
    }
    | {"mtf.aligned"}
)


USABLE_QUALITY = (DataQuality.OK, DataQuality.DEGRADED)


def snapshot_problem(
    snapshot: FeatureSnapshot, now: datetime, *, schema: str = FEATURE_SCHEMA
) -> str | None:
    """Why a consumer may not use this snapshot at `now`, or None when it may: it must
    be of the expected schema, of usable quality, and known and unexpired at `now`."""
    if snapshot.feature_schema != schema:
        return f"snapshot schema {snapshot.feature_schema} is not {schema}"
    if snapshot.quality not in USABLE_QUALITY:
        return f"snapshot quality {snapshot.quality}"
    if not live(snapshot.available_at, snapshot.valid_until, now):
        return f"snapshot not live at {now.isoformat()}"
    return None


def build_feature_snapshot(
    symbol: str,
    bars_by_tf: Mapping[Timeframe, Sequence[CandleClosed]],
    now: datetime,
    techniques: Sequence[Technique] = DEFAULT_TECHNIQUES,
) -> FeatureSnapshot:
    known: dict[Timeframe, list[CandleClosed]] = {}
    for tf, bars in bars_by_tf.items():
        if any(b.timeframe != tf or b.symbol != symbol for b in bars):
            raise ValueError(f"bars filed under {tf} must be {symbol} {tf} bars")
        known[tf] = sorted((b for b in bars if b.available_at <= now), key=lambda b: b.open_time)
    if not any(known.values()):
        raise ValueError("no bar was available at `now`")

    values: dict[str, Decimal | None] = {}
    missing_tf = stale = degraded = False
    valid_until: list[datetime] = []
    directions: list[Decimal | None] = []
    for tf, bars in known.items():
        for tech in techniques:
            obs = observe(tech, bars, now) if bars else None
            got = {} if obs is None else obs.values
            for key in tech.outputs:
                values[_key(tf, tech.technique_id, key)] = got.get(key)
            if tech.catalog_id == "CAT-T03-07":
                directions.append(got.get("direction"))
            if obs is None:
                missing_tf = True
            elif QualityFlag.STALE in obs.quality_flags:
                stale = True
            elif not obs.values:
                degraded = True
            else:
                valid_until.append(obs.valid_until)
    known_dirs = [d for d in directions if d is not None]
    if not directions or len(known_dirs) < len(directions):
        values["mtf.aligned"] = None
    else:
        values["mtf.aligned"] = Decimal(int(known_dirs[0] != 0 and len(set(known_dirs)) == 1))

    quality = (
        DataQuality.MISSING if missing_tf
        else DataQuality.STALE if stale
        else DataQuality.DEGRADED if degraded
        else DataQuality.OK
    )  # fmt: skip
    return FeatureSnapshot(
        symbol=symbol,
        feature_schema=FEATURE_SCHEMA,
        values=values,
        quality=quality,
        as_of=max(bars[-1].close_time for bars in known.values() if bars),
        available_at=now,
        valid_until=max(now, min(valid_until, default=now)),
    )
