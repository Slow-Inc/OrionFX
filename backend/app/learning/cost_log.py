"""Measured spread per era, beside the stressed cost (#128, protocol v1 §6; information only).

For each calendar year (an era), the median of the per-minute close spread
(`ask_close - bid_close`, the Dukascopy source's spread) and the median of that spread over
ATR_M15, the ATR of the last M15 bar closed by that minute (Wilder, `atr_n` bars). A minute
with no spread, or before ATR_M15 exists, is left out of the ratio.

This is a measurement to report beside `STRESSED_COST` (#129). It never changes that
bound or the V2 rule. Statistics never read the protocol's sealed windows: every minute at
or after `sealed_from` is left out (`SEALED_FROM`, #132).

**Combined-source rule** (`spread_profile`): HistData bars (the bulk M1 corpus) carry no
spread. Where Dukascopy's measured spread exists (the recent era), its median per
(year, month, UTC hour of day) is a **cost input only**: it may price a modelled cost in
the backtest or a cost report for a HistData bar of the same month and hour. It is never
a feature, never joined onto a training row, and never lowers `STRESSED_COST`, which the
protocol gates use. Months without Dukascopy data have no profile entry, and the stressed
spread applies. TODO(#131): the system backtest's MODELLED cost takes this profile.
"""

import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.contracts import CandleClosed
from app.market_data.port import M1Bar
from app.market_data.techniques.indicators import true_ranges, wilder

__all__ = ["EraCost", "era_costs", "spread_profile"]


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
    """`m1` and `m15` in time order (as the source and the store give them). One era is held
    in memory at a time: spreads as counts, ratios as a list."""
    bars = [b for b in m15 if b.close_time <= sealed_from]
    atrs = wilder(true_ranges(bars), atr_n) if len(bars) > atr_n else []
    closes = [b.close_time for b in bars[atr_n:]]  # atrs[k] is known at closes[k]
    out: list[EraCost] = []
    era: int | None = None
    spreads: Counter[Decimal] = Counter()
    ratios: list[Decimal] = []
    k = -1  # the last ATR known by the current minute
    for b in m1:
        if b.spread is None or b.open_time >= sealed_from:
            continue
        if b.open_time.year != era:
            if era is not None:
                out.append(_era(era, spreads, ratios))
            era, spreads, ratios = b.open_time.year, Counter(), []
        spreads[b.spread] += 1
        while k + 1 < len(closes) and closes[k + 1] <= b.open_time:
            k += 1
        if k >= 0 and atrs[k] > 0:
            ratios.append(b.spread / atrs[k])
    if era is not None:
        out.append(_era(era, spreads, ratios))
    return out


def _era(era: int, spreads: Counter[Decimal], ratios: list[Decimal]) -> EraCost:
    n = spreads.total()
    return EraCost(era, n, _counted_median(spreads, n),
                   statistics.median(ratios) if ratios else None)  # fmt: skip


def _counted_median(counts: Counter[Decimal], n: int) -> Decimal:
    """The median of the multiset `counts` (n values) without expanding it."""
    lo_rank, hi_rank = (n - 1) // 2, n // 2
    seen, lo = 0, None
    for value in sorted(counts):
        seen += counts[value]
        if lo is None and seen > lo_rank:
            lo = value
        if seen > hi_rank:
            low = value if lo is None else lo
            return value if low == value else (low + value) / 2
    raise ValueError("no values")


def spread_profile(
    m1: Iterable[M1Bar], *, sealed_from: datetime
) -> dict[tuple[int, int, int], Decimal]:
    """Median close spread per (year, month, UTC hour) from a source that has one
    (Dukascopy), before `sealed_from`: a cost input for spread-less bars, never a feature."""
    counts: dict[tuple[int, int, int], Counter[Decimal]] = {}
    for b in m1:
        if b.spread is None or b.open_time >= sealed_from:
            continue
        t = b.open_time
        counts.setdefault((t.year, t.month, t.hour), Counter())[b.spread] += 1
    return {k: _counted_median(c, c.total()) for k, c in sorted(counts.items())}
