"""Price-action techniques CAT-T03-01..07 (#31; DAT-02, UST-03).

All derived from closed bars only, and all run through `observe` like the
indicators, so they get the same WARMUP / GAP_IN_WINDOW / STALE / look-ahead
guards and lineage. A swing at bar i exists only once the n bars after it have
closed, so pivots never repaint. Structure, support/resistance and the last
swing levels read one shared swing window, so they describe the same pivots.
Parameters are part of each technique id; all thresholds are [PROPOSED]
(master plan: thresholds TBD). The multi-timeframe row (CAT-T03-07) is the
per-timeframe `direction` technique; agreement across timeframes is computed by
the feature snapshot from those observations.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.core.contracts import CandleClosed
from app.market_data.techniques.indicators import (
    OptValues,
    Technique,
    Values,
    adx_lookback,
    dmi,
    true_ranges,
    wilder,
)

D = Decimal
ZERO = D(0)
SWING_N = 2
SWING_LOOKBACK = 100
SR_TOLERANCE = D("0.50")  # USD per ounce for XAUUSD
SR_MIN_TOUCHES = 2
TREND_ADX, RANGE_ADX = D(25), D(20)
VOLATILE_ATR_RATIO = D("1.5")
DIRECTION_BARS = 10


def _flag(cond: bool) -> Decimal:
    return D(int(cond))


def _sign(x: Decimal) -> Decimal:
    return D((x > 0) - (x < 0))


@dataclass(frozen=True, slots=True)
class Swing:
    index: int
    kind: Literal["high", "low"]
    price: Decimal


def swings(bars: Sequence[CandleClosed], n: int = SWING_N) -> list[Swing]:
    """Confirmed swing highs/lows: strictly beyond the n closed bars on each side."""
    out: list[Swing] = []
    for i in range(n, len(bars) - n):
        others = (*bars[i - n : i], *bars[i + 1 : i + n + 1])
        if all(bars[i].high > b.high for b in others):
            out.append(Swing(i, "high", bars[i].high))
        if all(bars[i].low < b.low for b in others):
            out.append(Swing(i, "low", bars[i].low))
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
) -> OptValues:
    """Levels are clusters of confirmed swing prices within `tolerance`, touched at
    least SR_MIN_TOUCHES times; reports the nearest below and above the last close.
    None means no such level exists, which is a valid answer, not missing data."""
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
    body, rng = abs(c.close - c.open), c.high - c.low
    upper, lower = c.high - max(c.open, c.close), min(c.open, c.close) - c.low
    return {
        "bullish_engulfing": _flag(
            p.close < p.open and c.close > c.open and c.open <= p.close and c.close >= p.open
        ),
        "bearish_engulfing": _flag(
            p.close > p.open and c.close < c.open and c.open >= p.close and c.close <= p.open
        ),
        "doji": _flag(rng > 0 and body <= rng / 10),
        "hammer": _flag(body > 0 and lower >= 2 * body and upper <= body),
        "shooting_star": _flag(body > 0 and upper >= 2 * body and lower <= body),
    }


def last_bar(bars: Sequence[CandleClosed]) -> Values:
    b = bars[-1]
    rng = b.high - b.low
    return {
        "direction": _sign(b.close - b.open),
        "body_to_range": abs(b.close - b.open) / rng if rng else ZERO,
        "close_in_range": (b.close - b.low) / rng if rng else D("0.5"),
    }


def swing_features(bars: Sequence[CandleClosed], n: int, tolerance: Decimal) -> OptValues:
    found = swings(bars, n)
    highs = [s.price for s in found if s.kind == "high"]
    lows = [s.price for s in found if s.kind == "low"]
    return {
        "structure": D(market_structure(found)),
        "last_swing_high": highs[-1] if highs else None,
        "last_swing_low": lows[-1] if lows else None,
        **support_resistance(found, last_close=bars[-1].close, tolerance=tolerance),
    }


def regime(bars: Sequence[CandleClosed], adx_n: int, atr_n: int) -> Values:
    adx = dmi(bars, adx_n)["adx"]
    atrs = wilder(true_ranges(bars), atr_n)
    mean_atr = sum(atrs, ZERO) / len(atrs)
    ratio = atrs[-1] / mean_atr if mean_atr else ZERO
    return {
        "trend": _flag(adx >= TREND_ADX),
        "range": _flag(adx < RANGE_ADX),
        "volatile": _flag(ratio >= VOLATILE_ATR_RATIO),
        "adx": adx,
        "atr_ratio": ratio,
    }


def direction(bars: Sequence[CandleClosed]) -> Values:
    return {"direction": _sign(bars[-1].close - bars[0].close)}


SR_KEYS = ("support", "support_touches", "resistance", "resistance_touches")


def make_price_action(
    *,
    swing_n: int = SWING_N,
    swing_lookback: int = SWING_LOOKBACK,
    sr_tolerance: Decimal = SR_TOLERANCE,
    adx_n: int = 14,
    atr_n: int = 14,
    direction_bars: int = DIRECTION_BARS,
) -> dict[str, Technique]:
    """CAT-T03 techniques; parameters are part of each technique's identity. The
    S/R, structure and swing rows share one window and one swing computation."""
    swing_id = f"swings_{swing_n}_{swing_lookback}_{sr_tolerance.normalize()}"

    def swing_tech(catalog_id: str, keys: tuple[str, ...]) -> Technique:
        return Technique(
            f"{swing_id}.{catalog_id[-2:]}",
            catalog_id,
            lookback=swing_lookback,
            unit="price",
            outputs=keys,
            compute=lambda b: {
                k: v for k, v in swing_features(b, swing_n, sr_tolerance).items() if k in keys
            },
        )

    techniques = [
        Technique("bar_shape", "CAT-T03-01", lookback=1, unit="ratio",
                  outputs=("direction", "body_to_range", "close_in_range"), compute=last_bar),
        Technique("patterns", "CAT-T03-02", lookback=2, unit="flag",
                  outputs=("bullish_engulfing", "bearish_engulfing", "doji", "hammer",
                           "shooting_star"), compute=candle_patterns),
        swing_tech("CAT-T03-03", SR_KEYS),
        swing_tech("CAT-T03-04", ("structure",)),
        swing_tech("CAT-T03-05", ("last_swing_high", "last_swing_low")),
        Technique(f"regime_{adx_n}_{atr_n}", "CAT-T03-06",
                  lookback=adx_lookback(max(adx_n, atr_n)), unit="flag",
                  outputs=("trend", "range", "volatile", "adx", "atr_ratio"),
                  compute=lambda b: regime(b, adx_n, atr_n)),
        Technique(f"direction_{direction_bars}", "CAT-T03-07", lookback=direction_bars,
                  unit="direction", outputs=("direction",), compute=direction),
    ]  # fmt: skip
    return {t.catalog_id: t for t in techniques}
