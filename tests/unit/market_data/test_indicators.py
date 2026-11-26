"""M02 slice B: indicator techniques CAT-T01-01..09 and tick volume CAT-T09-01 (#30;
UST-02, UST-03). Reference values come from an independent float implementation
written here, compared within a tolerance."""

import itertools
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.market_data.techniques.indicators import make_library, observe

D = Decimal
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
CLOSES = [
    2000.0, 2002.5, 2001.0, 2004.0, 2003.2, 2006.8, 2005.1, 2009.4, 2008.0, 2007.2,
    2010.5, 2012.9, 2011.0, 2015.3, 2013.8, 2016.1, 2018.4, 2017.0, 2019.9, 2021.2,
    2020.1, 2023.5, 2022.0, 2025.7, 2024.3, 2026.0, 2029.2, 2027.5, 2030.8, 2032.1,
    2031.0, 2033.6, 2035.2, 2034.0, 2036.9, 2038.3, 2037.1, 2040.0, 2039.2, 2041.5,
]  # fmt: skip


def _bars(closes: list[float] = CLOSES, flags_at: int | None = None) -> list[CandleClosed]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev + (4.0 if i % 7 == 3 else 0.0)  # some bars gap away from the last close
        hi, lo = max(o, c) + 0.6 + (i % 3) * 0.1, min(o, c) - 0.5 - (i % 2) * 0.2
        if i % 6 == 5:  # outside bars: both directional moves are positive
            hi, lo = hi + 1.0, lo - 6.0  # the down move is larger
        t = T0 + timedelta(minutes=5 * i)
        out.append(
            CandleClosed(
                symbol="XAUUSD",
                timeframe=Timeframe.M5,
                open_time=t,
                close_time=t + timedelta(minutes=5),
                open=D(str(o)),
                high=D(str(round(hi, 2))),
                low=D(str(round(lo, 2))),
                close=D(str(c)),
                volume=D(10 + i),
                available_at=t + timedelta(minutes=5, seconds=1),
                quality_flags=["GAP"] if flags_at == i else [],
            )
        )
        prev = c
    return out


BARS = _bars()
NOW = BARS[-1].close_time + timedelta(seconds=2)
LIB = make_library(
    sma_n=5, ema_n=5, wma_n=5, rsi_n=5, macd_fast=3, macd_slow=6, macd_signal=4,
    atr_n=5, adx_n=5, bb_n=5,
)  # fmt: skip


# --- independent float references ---------------------------------------------------------------


def _f(bars: list[CandleClosed]) -> tuple[list[float], list[float], list[float]]:
    return (
        [float(b.close) for b in bars],
        [float(b.high) for b in bars],
        [float(b.low) for b in bars],
    )


def ref_ema(xs: list[float], n: int) -> list[float]:
    out = [sum(xs[:n]) / n]
    for x in xs[n:]:
        out.append(out[-1] + 2 / (n + 1) * (x - out[-1]))
    return out


def ref_wilder(xs: list[float], n: int) -> list[float]:
    out = [sum(xs[:n]) / n]
    for x in xs[n:]:
        out.append((out[-1] * (n - 1) + x) / n)
    return out


def ref_tr(c: list[float], h: list[float], lo: list[float]) -> list[float]:
    return [
        max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))
    ]


def _w(tid: str) -> list[CandleClosed]:
    """The bars a technique reads: exactly its lookback."""
    return BARS[-LIB[tid].lookback :]


def _value(tid: str, key: str) -> float:
    obs = observe(LIB[tid], BARS, NOW)
    assert obs.quality_flags == [] or tid == "tick_volume", obs.quality_flags
    v = obs.values[key]
    assert v is not None
    return float(v)


def test_sma_wma_ema() -> None:
    c, _, _ = _f(_w("ema_5"))
    assert _value("sma_5", "sma") == pytest.approx(sum(c[-5:]) / 5, abs=1e-9)
    assert _value("wma_5", "wma") == pytest.approx(
        sum(x * (i + 1) for i, x in enumerate(c[-5:])) / 15, abs=1e-9
    )
    assert _value("ema_5", "ema") == pytest.approx(ref_ema(c, 5)[-1], abs=1e-9)


def test_rsi() -> None:
    c, _, _ = _f(_w("rsi_5"))
    d = [b - a for a, b in itertools.pairwise(c)]
    g = ref_wilder([max(x, 0) for x in d], 5)[-1]
    lo = ref_wilder([max(-x, 0) for x in d], 5)[-1]
    assert _value("rsi_5", "rsi") == pytest.approx(100 - 100 / (1 + g / lo), abs=1e-9)


def test_macd() -> None:
    c, _, _ = _f(_w("macd_3_6_4"))
    fast, slow = ref_ema(c, 3)[3:], ref_ema(c, 6)
    line = [f - s for f, s in zip(fast, slow, strict=True)]
    sig = ref_ema(line, 4)[-1]
    assert _value("macd_3_6_4", "macd") == pytest.approx(line[-1], abs=1e-9)
    assert _value("macd_3_6_4", "signal") == pytest.approx(sig, abs=1e-9)
    assert _value("macd_3_6_4", "histogram") == pytest.approx(line[-1] - sig, abs=1e-9)


def test_atr_adx_dmi() -> None:
    c, h, lo = _f(_w("atr_5"))
    assert _value("atr_5", "atr") == pytest.approx(ref_wilder(ref_tr(c, h, lo), 5)[-1], abs=1e-9)
    c, h, lo = _f(_w("adx_5"))
    tr = ref_tr(c, h, lo)
    pdm, mdm = [], []
    for i in range(1, len(c)):
        up, down = h[i] - h[i - 1], lo[i - 1] - lo[i]
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
    trs = ref_wilder(tr, 5)
    pdi = [100 * a / t for a, t in zip(ref_wilder(pdm, 5), trs, strict=True)]
    mdi = [100 * a / t for a, t in zip(ref_wilder(mdm, 5), trs, strict=True)]
    dx = [100 * abs(p - m) / (p + m) for p, m in zip(pdi, mdi, strict=True)]
    assert _value("dmi_5", "plus_di") == pytest.approx(pdi[-1], abs=1e-9)
    assert _value("dmi_5", "minus_di") == pytest.approx(mdi[-1], abs=1e-9)
    assert _value("adx_5", "adx") == pytest.approx(ref_wilder(dx, 5)[-1], abs=1e-9)


def test_bollinger() -> None:
    c, _, _ = _f(_w("bb_5_2"))
    w = c[-5:]
    mid = sum(w) / 5
    sd = math.sqrt(sum((x - mid) ** 2 for x in w) / 5)
    assert _value("bb_5_2", "middle") == pytest.approx(mid, abs=1e-9)
    assert _value("bb_5_2", "upper") == pytest.approx(mid + 2 * sd, abs=1e-9)
    assert _value("bb_5_2", "lower") == pytest.approx(mid - 2 * sd, abs=1e-9)


def test_tick_volume_is_labelled_as_broker_ticks() -> None:
    obs = observe(LIB["tick_volume"], BARS, NOW)
    assert obs.values == {"tick_volume": D(10 + len(BARS) - 1)}
    assert obs.unit == "broker_ticks"
    assert "TICK_COUNT_NOT_TRADED_VOLUME" in obs.quality_flags


# --- UST-03: no false valid signal (every technique) ----------------------------------------

ALL = sorted(LIB)


@pytest.mark.parametrize("tid", ALL)
def test_warmup_gives_no_values(tid: str) -> None:
    tech = LIB[tid]
    if tech.lookback == 1:
        pytest.skip("a one-bar technique has no warm-up")
    short = BARS[-(tech.lookback - 1) :]
    obs = observe(tech, short, NOW)
    assert obs.values == {} and "WARMUP" in obs.quality_flags


@pytest.mark.parametrize("tid", ALL)
def test_gap_inside_the_window_gives_no_values(tid: str) -> None:
    obs = observe(LIB[tid], _bars(flags_at=len(CLOSES) - 1), NOW)
    assert obs.values == {} and "GAP_IN_WINDOW" in obs.quality_flags


@pytest.mark.parametrize("tid", ALL)
def test_non_contiguous_bars_give_no_values(tid: str) -> None:
    tech = LIB[tid]
    holed = [*BARS[: -tech.lookback - 1], *BARS[-tech.lookback :]]
    if tech.lookback == 1:
        holed = [BARS[-1]]  # one bar cannot be non-contiguous
        assert observe(tech, holed, NOW).values
        return
    window = [*BARS[-tech.lookback - 1 : -2], BARS[-1]]  # drops one bar inside the window
    obs = observe(tech, window, NOW)
    assert obs.values == {} and "GAP_IN_WINDOW" in obs.quality_flags
    assert observe(tech, holed, NOW).values  # a hole before the window does not matter


@pytest.mark.parametrize("tid", ALL)
def test_expired_input_gives_no_values(tid: str) -> None:
    obs = observe(LIB[tid], BARS, BARS[-1].close_time + timedelta(minutes=5))
    assert obs.values == {} and "STALE" in obs.quality_flags


def test_gap_before_the_lookback_does_not_matter() -> None:
    assert observe(LIB["ema_5"], _bars(flags_at=3), NOW) == observe(LIB["ema_5"], BARS, NOW)


def test_value_does_not_depend_on_extra_history() -> None:
    for tech in LIB.values():
        assert observe(tech, BARS, NOW) == observe(tech, BARS[-tech.lookback :], NOW)


def test_asking_before_the_bar_was_available_is_look_ahead() -> None:
    with pytest.raises(ValueError, match="look-ahead"):
        observe(LIB["sma_5"], BARS, BARS[-1].close_time)


def test_unknown_volume_is_not_reported_as_zero() -> None:
    bars = [*BARS[:-1], BARS[-1].model_copy(update={"volume": None})]
    obs = observe(LIB["tick_volume"], bars, NOW)
    assert obs.values == {} and "MISSING_INPUT" in obs.quality_flags


def test_bollinger_id_does_not_depend_on_decimal_spelling() -> None:
    assert "bb_20_2" in make_library(bb_k=D("2.0"))


def test_observation_carries_lineage_and_validity() -> None:
    obs = observe(LIB["sma_5"], BARS, NOW)
    assert obs.technique_id == "sma_5" and obs.technique_version == "1.0.0"
    assert obs.available_at == BARS[-1].available_at
    assert obs.valid_until == BARS[-1].close_time + timedelta(minutes=5)
    assert obs.input_refs[-1].endswith(BARS[-1].open_time.isoformat())


def test_same_bars_same_observation() -> None:
    assert observe(LIB["adx_5"], BARS, NOW) == observe(LIB["adx_5"], list(BARS), NOW)


def test_mixed_timeframes_are_refused() -> None:
    other = BARS[-1].model_copy(update={"timeframe": Timeframe.M15})
    with pytest.raises(ValueError):
        observe(LIB["sma_5"], [*BARS[:-1], other], NOW)


def test_every_mvp_indicator_row_has_a_technique() -> None:
    ids = {t.catalog_id for t in make_library().values()}
    assert ids == {f"CAT-T01-0{i}" for i in range(1, 10)} | {"CAT-T09-01"}
