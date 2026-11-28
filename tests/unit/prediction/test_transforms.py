"""The M04 stationary transform path (#129; research backlog item 1). One function turns
an M02 snapshot and the bars it was built from into features that do not depend on the
price level, with their own schema version; training and live prediction both call it.
Session and liquidity features come from the gold bars alone, point in time and
session-aware."""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.contracts import CandleClosed, FeatureSnapshot
from app.core.enums import DataQuality, Timeframe
from app.market_data.features import FEATURE_SCHEMA, build_feature_snapshot, feature_key
from app.market_data.session import XAUUSD_NY_1700
from app.prediction.transforms import (
    LEVEL_RELATIVE,
    SESSION_LOOKBACK,
    SPREAD_WINDOW,
    TRANSFORM_SCHEMA,
    transform,
    transform_keys,
)

D = Decimal
M5, M15 = Timeframe.M5, Timeframe.M15
STEP = {M5: timedelta(minutes=5), M15: timedelta(minutes=15)}
START = datetime(2026, 1, 5, tzinfo=UTC)  # Monday 00:00 UTC
AS_OF = datetime(2026, 1, 7, 15, 0, tzinfo=UTC)  # Wednesday
ATR_M15 = feature_key(M15, "CAT-T01-06", "atr")

Price = Callable[[Decimal], Decimal]


def _series(tf: Timeframe, end: datetime, *, price: Price = lambda x: x,
            spread: Price = lambda x: x) -> list[CandleClosed]:  # fmt: skip
    """A deterministic 24/7 walk from START to `end`, every price mapped by `price`."""
    out = []
    t, i = START, 0
    while t + STEP[tf] <= end:
        mid = 2000 + 10 * math.sin(i / 37) + 3 * math.sin(i / 7) + i * 0.01
        o, c = D(f"{mid:.2f}"), D(f"{mid + 0.4 * math.sin(i):.2f}")
        hi = max(o, c) + D(f"{0.3 + 0.2 * abs(math.sin(i / 3)):.2f}")
        lo = min(o, c) - D(f"{0.25 + 0.2 * abs(math.cos(i / 5)):.2f}")
        out.append(CandleClosed(
            symbol="XAUUSD", timeframe=tf, open_time=t, close_time=t + STEP[tf],
            open=price(o), high=price(hi), low=price(lo), close=price(c), volume=D(10 + i % 7),
            spread=spread(D("0.20") + D("0.05") * (i % 3)), available_at=t + STEP[tf],
        ))  # fmt: skip
        t, i = t + STEP[tf], i + 1
    return out


def _transformed(price: Price = lambda x: x, spread: Price = lambda x: x) -> FeatureSnapshot:
    bars = {tf: _series(tf, AS_OF, price=price, spread=spread) for tf in (M5, M15)}
    snap = build_feature_snapshot("XAUUSD", bars, AS_OF)
    assert snap.quality is DataQuality.OK
    return transform(snap, bars)


def test_the_output_has_its_own_schema_and_a_fixed_shape() -> None:
    out = _transformed()
    assert out.feature_schema == TRANSFORM_SCHEMA != FEATURE_SCHEMA
    assert list(out.values) == transform_keys([M5, M15])
    assert not any("tick_volume" in k for k in out.values)
    assert all(v is not None for v in out.values.values())  # warm data: everything known
    assert (out.as_of, out.quality) == (AS_OF, DataQuality.OK)


@pytest.mark.parametrize(
    ("price", "spread", "exempt"),
    [
        (lambda x: x + 1000, lambda x: x, LEVEL_RELATIVE),  # a shifted level
        (lambda x: x * 2, lambda x: x * 2, frozenset()),  # a doubled level (and spread)
    ],
    ids=["shift", "scale"],
)
def test_features_do_not_depend_on_the_price_level(
    price: Price, spread: Price, exempt: frozenset[str]
) -> None:
    base, moved = _transformed().values, _transformed(price, spread).values
    for key in transform_keys([M5, M15]):
        if key not in exempt:
            a, b = base[key], moved[key]
            assert a is not None and b is not None
            assert abs(a - b) <= D("1e-9"), key


def test_level_relative_features_are_the_only_ones_exempt_from_a_shift() -> None:
    assert {f"{tf}.{k}" for tf in Timeframe for k in ("atr_close", "log_return")} == LEVEL_RELATIVE


def test_the_hour_is_encoded_on_the_circle() -> None:
    v = _transformed().values  # 15:00 UTC
    angle = 2 * math.pi * 15 / 24
    assert v["time.hour_sin"] == D(math.sin(angle)).quantize(D("1e-12"))
    assert v["time.hour_cos"] == D(math.cos(angle)).quantize(D("1e-12"))


# --- session and liquidity features --------------------------------------------------------


def _bar(
    t: datetime,
    *,
    high: str = "2001",
    low: str = "1999",
    close: str = "2000",
    spread: str | None = "0.20",
    available_at: datetime | None = None,
) -> CandleClosed:
    return CandleClosed(
        symbol="XAUUSD", timeframe=M5, open_time=t, close_time=t + STEP[M5], open=D(close),
        high=D(high), low=D(low), close=D(close), volume=None,
        spread=None if spread is None else D(spread),
        available_at=available_at or t + STEP[M5],
    )  # fmt: skip


def _snap(as_of: datetime) -> FeatureSnapshot:
    return FeatureSnapshot(symbol="XAUUSD", feature_schema=FEATURE_SCHEMA,
                           values={ATR_M15: D(2)}, quality=DataQuality.OK, as_of=as_of,
                           available_at=as_of, valid_until=as_of + STEP[M5])  # fmt: skip


def _gold_week(edit: Callable[[datetime], dict[str, str]] = lambda t: {}) -> list[CandleClosed]:
    """M5 bars while XAUUSD trades, Thu 8 Jan 12:00 UTC to Mon 12 Jan 15:00 UTC."""
    t, end, out = datetime(2026, 1, 8, 12, tzinfo=UTC), datetime(2026, 1, 12, 15, tzinfo=UTC), []
    while t < end:
        if XAUUSD_NY_1700.expects(t, STEP[M5]):
            out.append(_bar(t, **edit(t)))  # type: ignore[arg-type]
        t += STEP[M5]
    return out


MON = datetime(2026, 1, 12, 15, tzinfo=UTC)  # Mon 10:00 New York
FRI_SPIKE = datetime(2026, 1, 9, 15, tzinfo=UTC)  # Friday's session
SUN_SPIKE = datetime(2026, 1, 11, 23, 30, tzinfo=UTC)  # Sunday evening: today's session
THU_SPIKE = datetime(2026, 1, 8, 20, tzinfo=UTC)  # Thursday 15:00 New York: the session before


def _spikes(t: datetime) -> dict[str, str]:
    if t == FRI_SPIKE:
        return {"high": "2010", "low": "1995"}
    if t in (SUN_SPIKE, THU_SPIKE):
        return {"high": "2030", "low": "1970"}
    return {}


def test_the_previous_session_is_the_last_completed_trading_day() -> None:
    v = transform(_snap(MON), {M5: _gold_week(_spikes)}, session=XAUUSD_NY_1700).values
    # previous session: Thu 18:00 to Fri 17:00 New York; close 2000, ATR_M15 2
    assert v["session.prev_high_atr"] == D("-5")  # (2000 - 2010) / 2
    assert v["session.prev_low_atr"] == D("2.5")  # (2000 - 1995) / 2


def test_without_a_schedule_the_session_is_the_utc_day() -> None:
    v = transform(_snap(MON), {M5: _gold_week(_spikes)}).values
    assert v["session.prev_high_atr"] == D("-15")  # Sunday 11 Jan UTC holds the 2030 spike


def test_a_window_too_short_for_the_previous_session_gives_none() -> None:
    bars = [b for b in _gold_week(_spikes) if b.open_time >= FRI_SPIKE]
    v = transform(_snap(MON), {M5: bars}, session=XAUUSD_NY_1700).values
    assert v["session.prev_high_atr"] is None and v["session.london_ny_vol"] is None


def test_session_features_are_point_in_time() -> None:
    bars = _gold_week(_spikes)
    base = transform(_snap(MON), {M5: bars}, session=XAUUSD_NY_1700).values
    # bars that became available after the snapshot change nothing, however extreme
    later = [_bar(MON + k * STEP[M5], high="2500", low="1500", close="2400", spread="9")
             for k in range(3)]  # fmt: skip
    after = transform(_snap(MON), {M5: [*bars, *later]}, session=XAUUSD_NY_1700).values
    assert after == base
    # the last bar is read when it was available, and not when it arrived late
    last = bars[-1].open_time

    def run(*extra: CandleClosed) -> dict[str, Decimal | None]:
        return transform(_snap(MON), {M5: [*bars[:-1], *extra]}, session=XAUUSD_NY_1700).values

    assert run(_bar(last, high="2600", close="2300")) != base
    assert run(_bar(last, high="2600", close="2300", available_at=MON + STEP[M5])) == run()


def test_the_london_new_york_volatility_ratio_follows_local_time() -> None:
    # Friday 9 Jan (winter): London 08:00-17:00 = 08-17 UTC; New York 08:00-17:00 = 13-22 UTC
    def ranges(t: datetime) -> dict[str, str]:
        if t.date() != FRI_SPIKE.date():
            return {}
        width = 4 if 8 <= t.hour < 13 else 2 if 13 <= t.hour < 17 else 1
        return {"high": str(2000 + D(width) / 2), "low": str(2000 - D(width) / 2)}

    v = transform(_snap(MON), {M5: _gold_week(ranges)}, session=XAUUSD_NY_1700).values
    london = (60 * 4 + 48 * 2) / D(108)  # 5 h at 4, 4 h at 2
    new_york = (48 * 2 + 60 * 1) / D(108)  # 4 h at 2, then 5 h to the 17:00 close at 1
    assert london / new_york == D(336) / D(156)
    assert v["session.london_ny_vol"] == (D(336) / D(156)).quantize(D("1e-12"))


def test_spread_anomaly_is_the_spread_over_its_rolling_median() -> None:
    bars = _gold_week()
    wide = [*bars[:-1], _bar(bars[-1].open_time, spread="0.60")]
    v = transform(_snap(MON), {M5: wide}, session=XAUUSD_NY_1700).values
    assert v["session.spread_anomaly"] == D(3)
    assert v["M5.spread_atr"] is None  # no M5 ATR in this snapshot


def test_a_missing_spread_is_unknown_never_zero() -> None:
    bars = _gold_week()
    gone = [*bars[:-1], _bar(bars[-1].open_time, spread=None)]
    snap = _snap(MON).model_copy(
        update={"values": {ATR_M15: D(2), feature_key(M5, "CAT-T01-06", "atr"): D(1)}}
    )
    v = transform(snap, {M5: gone}, session=XAUUSD_NY_1700).values
    assert v["session.spread_anomaly"] is None and v["M5.spread_atr"] is None
    short = transform(_snap(MON), {M5: bars[-(SPREAD_WINDOW - 1) :]}).values
    assert short["session.spread_anomaly"] is None  # warm-up, not a guess


def test_only_an_m02_snapshot_is_transformed() -> None:
    snap = _snap(MON).model_copy(update={"feature_schema": TRANSFORM_SCHEMA})
    with pytest.raises(ValueError, match="m02"):
        transform(snap, {M5: _gold_week()})


def test_the_lookback_reaches_the_previous_session_across_a_weekend() -> None:
    """SESSION_LOOKBACK counts bars, not hours, and a weekend has none: at Monday's last
    bar (16:55 New York) the previous session, Thu 18:00 to Fri 17:00, is 552 bars back."""
    t, end, bars = datetime(2026, 1, 8, 12, tzinfo=UTC), datetime(2026, 1, 12, 22, tzinfo=UTC), []
    while t < end:
        if XAUUSD_NY_1700.expects(t, STEP[M5]):
            bars.append(_bar(t, **_spikes(t)))  # type: ignore[arg-type]
        t += STEP[M5]
    v = transform(_snap(end), {M5: bars[-SESSION_LOOKBACK:]}, session=XAUUSD_NY_1700).values
    assert v["session.prev_high_atr"] == D("-5")  # Friday's 2010 spike, as with every bar


def test_no_bars_give_unknown_features_not_a_failure() -> None:
    v = transform(_snap(MON), {M5: []}, session=XAUUSD_NY_1700).values
    assert [k for k, x in v.items() if x is not None] == ["time.hour_sin", "time.hour_cos"]
    # no per-timeframe bars at all: only the hour and the (unknown) session features
    v = transform(_snap(MON), {}, session=XAUUSD_NY_1700).values
    assert [k for k, x in v.items() if x is not None] == ["time.hour_sin", "time.hour_cos"]
