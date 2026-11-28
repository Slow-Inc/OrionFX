"""Regime buckets for the system backtest (#131; protocol v1 draft §7).

A classifier tags a time with one bucket per dimension, from what was known at that
time only: the daily bars whose trading day had ended (`DailyBar.known_at <= t`). The
backtest hands it exactly those, so the classifier cannot see the future.

`protocol_v1_draft` implements the rule in `docs/research/2026-11-28-m04-protocol-v1.md`
§7, which is a DRAFT (not hashed). It is pluggable: a frozen protocol passes its own.
- **Volatility:** the latest day's realised volatility (root sum of squared intraday
  log returns) as a percentile of the trailing 252 days': low < 33, mid 33-66, high > 66.
- **Trend:** the z-score of the latest 20-day log return against the trailing 252
  20-day returns: above +1 trend-up, below -1 trend-down, otherwise range.
- **Session (New York time):** Asia 18:00-02:00, London 02:00-08:00, New York
  08:00-17:00, rollover 17:00-18:00.
- **Era:** 2008-2015, 2016-2021, 2022-end (before 2008: pre-2008).
With too little history a dimension is `unknown`, never guessed. Named stress windows
and news windows are not classified: their boundaries are not fixed in the draft, and
no point-in-time calendar source exists (#41 / OD-14).

A bucket with fewer than the floors' trades or trading days is NOT_COVERED: reported,
never a pass or a fail.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

D = Decimal
NEW_YORK = ZoneInfo("America/New_York")
TRAILING_DAYS = 252
TREND_DAYS = 20
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DailyBar:
    day: date
    """The trading day."""
    close: Decimal
    realised_vol: Decimal
    """Root sum of squared log returns of the day's driving bars."""
    known_at: datetime
    """When the day had ended and its last bar was available."""


RegimeClassifier = Callable[[datetime, Sequence[DailyBar]], Mapping[str, str]]
"""Tags at `t`, given the daily bars known at `t` (oldest first)."""


class Coverage(StrEnum):
    COVERED = "COVERED"
    NOT_COVERED = "NOT_COVERED"


@dataclass(frozen=True, slots=True)
class CoverageFloor:
    """Protocol v1 draft §7: a bucket needs 300 trades and 60 trading days."""

    min_trades: int = 300
    min_days: int = 60

    def coverage(self, trades: int, days: int) -> Coverage:
        enough = trades >= self.min_trades and days >= self.min_days
        return Coverage.COVERED if enough else Coverage.NOT_COVERED


def _percentile(value: Decimal, sample: Sequence[Decimal]) -> Decimal:
    """Percent of the sample at or below `value`."""
    return D(100) * sum(1 for v in sample if v <= value) / len(sample)


def volatility(days: Sequence[DailyBar]) -> str:
    if len(days) < TRAILING_DAYS + 1:
        return UNKNOWN
    pct = _percentile(
        days[-1].realised_vol, [d.realised_vol for d in days[-TRAILING_DAYS - 1 : -1]]
    )
    return "low" if pct < 33 else "high" if pct > 66 else "mid"


def trend(days: Sequence[DailyBar]) -> str:
    closes = [d.close for d in days[-(TRAILING_DAYS + TREND_DAYS + 1) :]]
    if len(closes) < TRAILING_DAYS + TREND_DAYS + 1:
        return UNKNOWN
    returns = [(closes[i] / closes[i - TREND_DAYS]).ln() for i in range(TREND_DAYS, len(closes))]
    trailing = returns[-TRAILING_DAYS - 1 : -1]
    mean = sum(trailing, D(0)) / len(trailing)
    sd = (sum(((r - mean) ** 2 for r in trailing), D(0)) / len(trailing)).sqrt()
    if sd == 0:
        return UNKNOWN
    z = (returns[-1] - mean) / sd
    return "trend-up" if z > 1 else "trend-down" if z < -1 else "range"


def session(t: datetime) -> str:
    local = t.astimezone(NEW_YORK).time()
    if time(2) <= local < time(8):
        return "london"
    if time(8) <= local < time(17):
        return "new-york"
    if time(17) <= local < time(18):
        return "rollover"
    return "asia"


def era(t: datetime) -> str:
    if t.year < 2008:
        return "pre-2008"
    return "2008-2015" if t.year < 2016 else "2016-2021" if t.year < 2022 else "2022-end"


def protocol_v1_draft(t: datetime, days: Sequence[DailyBar]) -> dict[str, str]:
    return {"era": era(t), "session": session(t), "volatility": volatility(days),
            "trend": trend(days)}  # fmt: skip
