"""Measured spread per era, beside the stressed cost (#128, protocol v1 §6; information only).

For each calendar year (an era), the median of the per-minute close spread
(`ask_close - bid_close`, the Dukascopy source's spread) and the median of that spread over
ATR_M15, the ATR of the last M15 bar closed by that minute (Wilder, `atr_n` bars). A minute
with no spread, or before ATR_M15 exists, is left out of the ratio.

This is a measurement to report beside `STRESSED_COST` (#129). It never changes that
bound or the V2 rule. Statistics never read the protocol's sealed windows: every minute at
or after `sealed_from` is left out (`SEALED_FROM`, #132).
"""

import bisect
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.contracts import CandleClosed
from app.market_data.port import M1Bar
from app.market_data.techniques.indicators import true_ranges, wilder

__all__ = ["EraCost", "era_costs"]


@dataclass(frozen=True, slots=True)
class EraCost:
    era: int
    minutes: int
    """Minutes with a spread in the era (before `sealed_from`)."""
    median_spread: Decimal
    median_spread_over_atr: Decimal | None
    """None if no minute in the era had an ATR_M15 yet."""


def era_costs(
    m1: Iterable[M1Bar],
    m15: Sequence[CandleClosed],
    *,
    sealed_from: datetime,
    atr_n: int = 14,
) -> list[EraCost]:
    bars = sorted((b for b in m15 if b.close_time <= sealed_from), key=lambda b: b.open_time)
    closes, atrs = [], []
    if len(bars) > atr_n:
        atrs = wilder(true_ranges(bars), atr_n)  # atrs[k] is known at bars[atr_n + k] close
        closes = [b.close_time for b in bars[atr_n:]]
    spreads: dict[int, list[Decimal]] = {}
    ratios: dict[int, list[Decimal]] = {}
    for b in m1:
        if b.spread is None or b.open_time >= sealed_from:
            continue
        era = b.open_time.year
        spreads.setdefault(era, []).append(b.spread)
        i = bisect.bisect_right(closes, b.open_time) - 1
        if i >= 0 and atrs[i] > 0:
            ratios.setdefault(era, []).append(b.spread / atrs[i])
    return [
        EraCost(
            era,
            len(s),
            statistics.median(s),
            statistics.median(ratios[era]) if era in ratios else None,
        )
        for era, s in sorted(spreads.items())
    ]
