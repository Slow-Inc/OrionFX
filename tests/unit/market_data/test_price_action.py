"""M02 slice C: price-action techniques CAT-T03-01..07 and the feature snapshot (#31;
DAT-02, DAT-04, UST-03). Swings are confirmed only by closed bars (no repaint);
the multi-timeframe view uses only higher-timeframe bars already available."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.contracts import CandleClosed, FeatureSnapshot
from app.core.enums import DataQuality, Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.market_data.features import FEATURE_SCHEMA, build_feature_snapshot
from app.market_data.techniques.indicators import observe
from app.market_data.techniques.price_action import (
    Swing,
    candle_patterns,
    last_bar,
    make_price_action,
    market_structure,
    regime,
    support_resistance,
    swings,
)

PA = make_price_action()

D = Decimal
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
BASE = 100.0  # keeps every test price positive, as the contract requires


def _bar(
    i: int, o: float, h: float, lo: float, c: float, tf: Timeframe = Timeframe.M5
) -> CandleClosed:
    o, h, lo, c = (round(x + BASE, 4) for x in (o, h, lo, c))
    size = TIMEFRAME_DURATION[tf]
    t = T0 + size * i
    return CandleClosed(
        symbol="XAUUSD",
        timeframe=tf,
        open_time=t,
        close_time=t + size,
        open=D(str(o)),
        high=D(str(h)),
        low=D(str(lo)),
        close=D(str(c)),
        volume=D(10),
        available_at=t + size + timedelta(seconds=1),
    )


def _series(highs: list[float], tf: Timeframe = Timeframe.M5) -> list[CandleClosed]:
    """Bars whose high follows `highs` and whose low is 2 below; close mid-range."""
    return [_bar(i, h - 1, h, h - 2, h - 1, tf) for i, h in enumerate(highs)]


# --- swings (CAT-T03-05) ----------------------------------------------------------------------


def test_swing_needs_closed_confirmation_bars() -> None:
    bars = _series([1, 2, 5, 3, 2])
    found = swings(bars, n=2)
    assert [(s.index, s.kind) for s in found if s.kind == "high"] == [(2, "high")]
    # with only one confirming bar after the peak, it is not a swing yet (no repaint)
    assert [s for s in swings(bars[:4], n=2) if s.kind == "high"] == []


def test_swing_low() -> None:
    bars = [_bar(i, p + 1, p + 2, p, p + 1) for i, p in enumerate([9, 7, 4, 6, 8])]
    assert [(s.index, s.price) for s in swings(bars, n=2) if s.kind == "low"] == [(2, D(104))]


# --- structure, S/R, patterns -----------------------------------------------------------------


def test_market_structure_up_down_and_mixed() -> None:
    up = _series([10, 12, 11, 9, 11, 14, 13, 11, 13, 16, 15, 13, 14])
    assert market_structure(swings(up, n=1)) == 1
    down = _series([20, 18, 19, 21, 19, 16, 17, 19, 17, 14, 15, 17, 16])
    assert market_structure(swings(down, n=1)) == -1
    assert market_structure([]) == 0


def test_support_resistance_from_clustered_swings() -> None:
    highs = [10, 12, 15, 12, 10, 12, 15.2, 12, 10, 11, 12]
    bars = _series(highs)
    levels = support_resistance(swings(bars, n=2), last_close=D(111), tolerance=D("0.5"))
    assert levels["resistance"] == D("115.1")  # two touches at 15 and 15.2
    assert levels["resistance_touches"] == 2


def test_candle_patterns() -> None:
    engulf = [_bar(0, 10, 10.5, 8.8, 9), _bar(1, 8.9, 11.2, 8.7, 11)]
    assert candle_patterns(engulf)["bullish_engulfing"] == 1
    doji = [_bar(0, 10, 11, 9, 10.5), _bar(1, 10, 11, 9, 10.02)]
    assert candle_patterns(doji)["doji"] == 1
    hammer = [_bar(0, 10, 10.2, 9.5, 9.6), _bar(1, 9.6, 9.75, 7, 9.7)]
    assert candle_patterns(hammer)["hammer"] == 1
    star = [_bar(0, 9.5, 9.8, 9.4, 9.7), _bar(1, 9.7, 12.4, 9.65, 9.8)]
    assert candle_patterns(star)["shooting_star"] == 1
    bear = [_bar(0, 9, 11.2, 8.8, 11), _bar(1, 11.1, 11.3, 8.7, 8.9)]
    assert candle_patterns(bear)["bearish_engulfing"] == 1
    assert candle_patterns(bear)["bullish_engulfing"] == 0


def test_bar_shape() -> None:
    values = last_bar([_bar(0, 10, 12, 8, 11)])
    assert values == {"direction": 1, "body_to_range": D("0.25"), "close_in_range": D("0.75")}
    assert last_bar([_bar(0, 10, 10, 10, 10)])["close_in_range"] == D("0.5")


def test_price_action_techniques_cover_the_mvp_rows() -> None:
    assert set(PA) == {f"CAT-T03-0{i}" for i in range(1, 8)}
    assert len({t.technique_id for t in PA.values()}) == 7


def test_regime_reports_trend_in_a_steady_climb() -> None:
    bars = [_bar(i, 100 + i, 101.5 + i, 99.5 + i, 101 + i) for i in range(120)]
    now = bars[-1].available_at
    obs = observe(PA["CAT-T03-06"], bars, now)
    assert obs.values["trend"] == 1 and obs.values["range"] == 0


def test_regime_range_and_volatility() -> None:
    flat = [_bar(i, 100 + (i % 2), 101.5, 99.5, 100 + ((i + 1) % 2)) for i in range(120)]
    values = regime(flat, 14, 14)
    assert values["range"] == 1 and values["trend"] == 0 and values["volatile"] == 0
    spike = [*flat[:-1], _bar(119, 100, 130, 90, 125)]  # one true range of 40 vs ~2
    assert regime(spike, 14, 14)["volatile"] == 1


def test_price_action_observations_refuse_warmup() -> None:
    bars = _series([1, 2, 3])
    obs = observe(PA["CAT-T03-04"], bars, bars[-1].available_at)
    assert obs.values == {} and "WARMUP" in obs.quality_flags


# --- multi-timeframe (CAT-T03-07) and the feature snapshot ------------------------------------


def _trend(tf: Timeframe, n: int, step: float, start: float = 2000) -> list[CandleClosed]:
    out = []
    for i in range(n):
        o, c = start + i * step, start + (i + 1) * step
        out.append(_bar(i, o, max(o, c) + 1, min(o, c) - 1, c, tf))
    return out


def _snap(bars: dict[Timeframe, list[CandleClosed]], now: datetime) -> FeatureSnapshot:
    return build_feature_snapshot("XAUUSD", bars, now)


def test_multi_timeframe_uses_only_available_higher_bars() -> None:
    m5 = _trend(Timeframe.M5, 120, 0.5)
    h1 = _trend(Timeframe.H1, 30, -2.0)  # most H1 bars lie in M5's future
    now = m5[-1].available_at
    snap = _snap({Timeframe.M5: m5, Timeframe.H1: h1}, now)
    used = [b for b in h1 if b.available_at <= now]
    assert snap == _snap({Timeframe.M5: m5, Timeframe.H1: used}, now)  # future H1 unseen
    assert snap.values["M5.direction_10.direction"] == 1
    assert snap.values["H1.direction_10.direction"] == -1
    assert snap.values["mtf.aligned"] == 0


def test_alignment_is_unknown_when_a_direction_is_unknown() -> None:
    m5 = _trend(Timeframe.M5, 120, 0.5)
    h1 = _trend(Timeframe.H1, 3, 0.5)  # not enough H1 bars for a direction yet
    snap = _snap({Timeframe.M5: m5, Timeframe.H1: h1}, m5[-1].available_at)
    assert snap.values["H1.direction_10.direction"] is None
    assert snap.values["mtf.aligned"] is None


def test_feature_snapshot_is_point_in_time_and_versioned() -> None:
    m5 = _trend(Timeframe.M5, 300, 0.1)
    now = m5[200].available_at
    snap = _snap({Timeframe.M5: m5}, now)
    assert snap.feature_schema == FEATURE_SCHEMA
    assert snap.as_of == m5[200].close_time  # the newest bar known at `now`
    assert snap.available_at == now <= snap.valid_until
    assert snap == _snap({Timeframe.M5: m5[:201]}, now)  # later bars never change it


def test_snapshot_shape_is_fixed_whatever_the_data_state() -> None:
    m5 = _trend(Timeframe.M5, 300, 0.1)
    warm = _snap({Timeframe.M5: m5[:10]}, m5[9].available_at)
    full = _snap({Timeframe.M5: m5}, m5[-1].available_at)
    assert set(warm.values) == set(full.values)
    assert warm.values["M5.sma_20.sma"] is None and full.values["M5.sma_20.sma"] is not None


def test_no_level_found_is_not_degraded() -> None:
    m5 = _trend(Timeframe.M5, 300, 0.1)  # a steady climb: no swing, no S/R level
    snap = _snap({Timeframe.M5: m5}, m5[-1].available_at)
    assert snap.values[f"M5.{make_price_action()['CAT-T03-03'].technique_id}.resistance"] is None
    assert snap.quality is DataQuality.OK


def test_feature_snapshot_quality_is_the_worst_input() -> None:
    m5 = _trend(Timeframe.M5, 300, 0.1)
    assert _snap({Timeframe.M5: m5[:10]}, m5[9].available_at).quality is DataQuality.DEGRADED
    later = m5[-1].available_at + timedelta(hours=1)
    assert _snap({Timeframe.M5: m5}, later).quality is DataQuality.STALE
    h1_absent = _snap({Timeframe.M5: m5, Timeframe.H1: []}, m5[-1].available_at)
    assert h1_absent.quality is DataQuality.MISSING


def test_bars_under_the_wrong_timeframe_are_refused() -> None:
    h1 = _trend(Timeframe.H1, 30, 1.0)
    with pytest.raises(ValueError, match="must be XAUUSD M5"):
        _snap({Timeframe.M5: h1}, h1[-1].available_at)


def test_feature_snapshot_with_no_available_bar_is_refused() -> None:
    with pytest.raises(ValueError):
        _snap({Timeframe.M5: []}, T0)


def test_expanding_swings_are_not_a_trend() -> None:
    found = [
        Swing(1, "high", D(110)),
        Swing(2, "low", D(100)),
        Swing(3, "high", D(112)),  # higher high
        Swing(4, "low", D(98)),  # but lower low
    ]
    assert market_structure(found) == 0


def test_a_level_touched_once_is_not_support_or_resistance() -> None:
    levels = support_resistance([Swing(0, "high", D(120))], last_close=D(110))
    assert levels["resistance"] is None and levels["support"] is None


def test_a_bullish_bar_that_does_not_engulf_is_not_engulfing() -> None:
    bars = [_bar(0, 10, 10.5, 8.8, 9), _bar(1, 9.2, 10, 9.1, 9.8)]
    assert candle_patterns(bars)["bullish_engulfing"] == 0
