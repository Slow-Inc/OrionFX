"""The market's move between two bar closes, on aligned bars (#125, PRD #120 slice 0b).

A label measures the price from the close of the bar ending at `as_of` to the close of
the bar ending at the horizon end. Those instants are on the recording timeframe's grid,
which an M15 or H1 bar need not share (a snapshot recorded on M5 at 10:05 has no M15 bar
closing then). So the move is read from the finest timeframe that has a bar closing at
both instants. A missing bar is never stretched over: no pair, no move.
"""

from datetime import datetime

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.core.time import TIMEFRAME_DURATION
from app.market_data.store import CandleStore

__all__ = ["FINEST_FIRST", "bars_closing_at", "data_frontier"]

FINEST_FIRST = (Timeframe.M5, Timeframe.M15, Timeframe.H1)


def bars_closing_at(
    candles: CandleStore, symbol: str, start: datetime, end: datetime, *, now: datetime
) -> tuple[CandleClosed, CandleClosed] | None:
    """The bars closing at `start` and at `end`, of one timeframe, known at `now`."""
    for tf in FINEST_FIRST:
        step = TIMEFRAME_DURATION[tf]
        first = candles.bar_at(symbol, tf, start - step, as_of=now)
        if first is None:
            continue
        last = candles.bar_at(symbol, tf, end - step, as_of=now)
        if last is not None:
            return first, last
    return None


def data_frontier(candles: CandleStore, symbol: str, *, now: datetime) -> datetime | None:
    """How far every stored timeframe's data reaches at `now`: the earliest of their
    newest bar opens. A bar that should close at or before the frontier and is still
    missing will not arrive. Taking the earliest means one timeframe running ahead of
    another (an import or repair done per timeframe) cannot make a late bar look lost."""
    opens = [o for tf in FINEST_FIRST
             if (o := candles.latest_open(symbol, tf, as_of=now)) is not None]  # fmt: skip
    return min(opens, default=None)
