"""Price-action techniques CAT-T03-01..07 (#31; DAT-02, UST-03).

All derived from closed bars only. A swing at bar i exists only once the n bars
after it have closed, so pivots never repaint. Structure, support/resistance and
patterns are built from those confirmed swings and closed bars; the regime comes
from ADX and ATR. The multi-timeframe view reads only bars that were available
at `now`, so an M5 decision never sees a future H1 close. All thresholds are
[PROPOSED] (master plan: thresholds TBD).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.market_data.techniques.indicators import Technique, Values, dmi, true_ranges, wilder

D = Decimal
ONE, ZERO = D(1), D(0)
SWING_N = 2
SR_TOLERANCE = D("0.50")  # USD per ounce for XAUUSD [PROPOSED]
SR_MIN_TOUCHES = 2
TREND_ADX, RANGE_ADX = D(25), D(20)
VOLATILE_ATR_RATIO = D("1.5")
DIRECTION_BARS = 10


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    kind: Literal["high", "low"]
    price: Decimal


def swings(bars: Sequence[CandleClosed], n: int = SWING_N) -> list[Swing]:
    """Confirmed swing highs/lows: strictly beyond the n closed bars on each side."""
    out: list[Swing] = []
    for i in range(n, len(bars) - n):
        left, right = bars[i - n : i], bars[i + 1 : i + n + 1]
        h, lo = bars[i].high, bars[i].low
        if all(h > b.high for b in (*left, *right)):
            out.append(Swing(i, "high", h))
        if all(lo < b.low for b in (*left, *right)):
            out.append(Swing(i, "low", lo))
    return out


def market_structure(found: Sequence[Swing]) -> int:
    """+1 higher highs and higher lows, -1 lower highs and lower lows, else 0."""
    highs = [s.price for s in found if s.kind == "high"][-2:]
    lows = [s.price for s in found if s.kind == "low"][-2:]
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return 1
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return -1
    return 0


def support_resistance(
    found: Sequence[Swing], *, last_close: Decimal, tolerance: Decimal = SR_TOLERANCE
) -> Values:
    """Levels are clusters of confirmed swing prices within `tolerance`, touched at
    least SR_MIN_TOUCHES times; reports the nearest below and above the last close."""
    levels: list[tuple[Decimal, int]] = []
    cluster: list[Decimal] = []
    for price in sorted(s.price for s in found):
        if cluster and price - cluster[0] > tolerance:
            levels.append((sum(cluster, ZERO) / len(cluster), len(cluster)))
            cluster = []
        cluster.append(price)
    if cluster:
        levels.append((sum(cluster, ZERO) / len(cluster), len(cluster)))
    strong = [(p, t) for p, t in levels if t >= SR_MIN_TOUCHES]
    below = max((lv for lv in strong if lv[0] <= last_close), default=None)
    above = min((lv for lv in strong if lv[0] > last_close), default=None)
    return {
        "support": None if below is None else below[0],
        "support_touches": None if below is None else D(below[1]),
        "resistance": None if above is None else above[0],
        "resistance_touches": None if above is None else D(above[1]),
    }


def candle_patterns(bars: Sequence[CandleClosed]) -> Values:
    """A fixed, documented set on the last two closed bars; 1 = present, 0 = absent."""
    p, c = bars[-2], bars[-1]
    body = abs(c.close - c.open)
    rng = c.high - c.low
    upper, lower = c.high - max(c.open, c.close), min(c.open, c.close) - c.low

    def flag(cond: bool) -> Decimal:
        return ONE if cond else ZERO

    return {
        "bullish_engulfing": flag(
            p.close < p.open and c.close > c.open and c.open <= p.close and c.close >= p.open
        ),
        "bearish_engulfing": flag(
            p.close > p.open and c.close < c.open and c.open >= p.close and c.close <= p.open
        ),
        "doji": flag(rng > 0 and body <= rng / 10),
        "hammer": flag(body > 0 and lower >= 2 * body and upper <= body),
        "shooting_star": flag(body > 0 and upper >= 2 * body and lower <= body),
    }


def _last_bar(bars: Sequence[CandleClosed]) -> Values:
    b = bars[-1]
    rng = b.high - b.low
    return {
        "direction": D((b.close > b.open) - (b.close < b.open)),
        "body_to_range": abs(b.close - b.open) / rng if rng else ZERO,
        "close_in_range": (b.close - b.low) / rng if rng else D("0.5"),
    }


def _swing_levels(bars: Sequence[CandleClosed]) -> Values:
    found = swings(bars)
    highs = [s.price for s in found if s.kind == "high"]
    lows = [s.price for s in found if s.kind == "low"]
    return {
        "last_swing_high": highs[-1] if highs else None,
        "last_swing_low": lows[-1] if lows else None,
    }


def regime(bars: Sequence[CandleClosed], n: int = 14) -> Values:
    adx = dmi(bars, n)["adx"]
    atrs = wilder(true_ranges(bars), n)
    mean_atr = sum(atrs, ZERO) / len(atrs)
    ratio = atrs[-1] / mean_atr if mean_atr else ZERO
    return {
        "trend": ONE if adx >= TREND_ADX else ZERO,
        "range": ONE if adx < RANGE_ADX else ZERO,
        "volatile": ONE if ratio >= VOLATILE_ATR_RATIO else ZERO,
        "adx": adx,
        "atr_ratio": ratio,
    }


PRICE_ACTION: dict[str, Technique] = {
    t.technique_id: t
    for t in [
        Technique("price_action", "CAT-T03-01", lookback=1, unit="ratio", compute=_last_bar),
        Technique("patterns", "CAT-T03-02", lookback=2, unit="flag", compute=candle_patterns),
        Technique(
            "sr",
            "CAT-T03-03",
            lookback=100,
            unit="price",
            compute=lambda b: support_resistance(swings(b), last_close=b[-1].close),
        ),
        Technique(
            "structure",
            "CAT-T03-04",
            lookback=50,
            unit="direction",
            compute=lambda b: {"structure": D(market_structure(swings(b)))},
        ),
        Technique("swings", "CAT-T03-05", lookback=50, unit="price", compute=_swing_levels),
        Technique("regime", "CAT-T03-06", lookback=6 * 14 + 1, unit="flag", compute=regime),
    ]
}


@dataclass(frozen=True, slots=True)
class MultiTimeframeView:
    inputs: dict[Timeframe, list[CandleClosed]]
    """The bars each timeframe contributed: only those available at `now`."""
    values: Values


def multi_timeframe(
    bars_by_tf: Mapping[Timeframe, Sequence[CandleClosed]], now: datetime
) -> MultiTimeframeView:
    """CAT-T03-07: direction per timeframe over its last DIRECTION_BARS available bars,
    and whether they all agree. A timeframe without two available bars gives None."""
    inputs = {tf: [b for b in bars if b.available_at <= now] for tf, bars in bars_by_tf.items()}
    values: Values = {}
    directions = []
    for tf, bars in inputs.items():
        window = bars[-DIRECTION_BARS:]
        if len(window) < 2:
            values[f"{tf}.direction"] = None
            continue
        delta = window[-1].close - window[0].close
        d = D((delta > 0) - (delta < 0))
        values[f"{tf}.direction"] = d
        directions.append(d)
    complete = len(directions) == len(inputs) and directions
    aligned = complete and directions[0] != 0 and len(set(directions)) == 1
    values["aligned"] = ONE if aligned else ZERO
    return MultiTimeframeView(inputs, values)
