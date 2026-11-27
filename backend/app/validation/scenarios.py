"""Deterministic synthetic market scenarios for AT-25 (#67; VAL-02, VAL-03).

Each generator returns closed bars with a known shape, so a replay can be run against
a regime on purpose: strong trend, range, false breakout, reversal, news spike, wide
spread. They are seeded: the same arguments always give the same bars. They test
behaviour under a regime; they are not market data and prove nothing about edge.
"""

import random
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.core.time import TIMEFRAME_DURATION

D = Decimal
CENT = D("0.01")
BASE = D(2000)
LATENCY = timedelta(seconds=1)
"""A bar becomes known just after it closes."""


class Scenario(StrEnum):
    STRONG_TREND = "STRONG_TREND"
    RANGE = "RANGE"
    FALSE_BREAKOUT = "FALSE_BREAKOUT"
    REVERSAL = "REVERSAL"
    NEWS_SPIKE = "NEWS_SPIKE"
    WIDE_SPREAD = "WIDE_SPREAD"


BANDED = (Scenario.RANGE, Scenario.FALSE_BREAKOUT, Scenario.WIDE_SPREAD, Scenario.NEWS_SPIKE)


def _path(kind: Scenario, n: int, rng: random.Random) -> list[Decimal]:
    """Close-to-close drift per bar (price units), before noise."""
    third = n // 3
    steps: list[Decimal] = []
    for i in range(n):
        if kind is Scenario.STRONG_TREND:
            drift = D("2.0")
        elif kind is Scenario.REVERSAL:
            drift = D("2.0") if i < n // 2 else D("-2.0")
        elif kind is Scenario.FALSE_BREAKOUT:
            # range, then a burst above it, then straight back inside
            burst, back = third <= i < third + 3, third + 3 <= i < third + 6
            drift = D("3.0") if burst else D("-3.0") if back else D(0)
        else:
            drift = D(0)
        if kind in BANDED:
            drift += D("-0.4") if i % 8 < 4 else D("0.4")  # oscillate inside a band
        steps.append(drift + D(str(round(rng.uniform(-0.6, 0.6), 2))))
    return steps


def generate(
    kind: Scenario,
    *,
    start: datetime,
    bars: int,
    timeframe: Timeframe = Timeframe.M15,
    seed: int = 7,
    symbol: str = "XAUUSD",
) -> list[CandleClosed]:
    rng = random.Random(f"{kind}:{seed}")
    step = TIMEFRAME_DURATION[timeframe]
    spike_at = bars // 2
    out: list[CandleClosed] = []
    price = BASE
    for i, move in enumerate(_path(kind, bars, rng)):
        open_ = price
        close = (price + move).quantize(CENT)
        wick = D("0.5") + abs(move) / 2
        if kind is Scenario.NEWS_SPIKE and i == spike_at:
            wick += D(25)  # a news candle: a huge range around an ordinary close
        spread = D("1.50") if kind is Scenario.WIDE_SPREAD else D("0.20")
        open_time = start + step * i
        out.append(CandleClosed(
            symbol=symbol, timeframe=timeframe, open_time=open_time,
            close_time=open_time + step, open=open_, close=close,
            high=(max(open_, close) + wick).quantize(CENT),
            low=(min(open_, close) - wick).quantize(CENT), volume=None, spread=spread,
            available_at=open_time + step + LATENCY,
        ))  # fmt: skip
        price = close
    return out
