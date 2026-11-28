"""Triple-barrier labels (#129; research backlog item 0). Prices are bid OHLC; a long
fills at ask (bid + spread) and exits at bid, a short the mirror image, every fill worse
by the slippage buffer. Barriers sit at +/- TARGET_ATR x ATR_M15 from the entry fill, so a
side that resolves at a barrier realises exactly +/- that distance. A bar touching both
barriers is SL first; a missing spread costs the locked buffer, never zero."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.decision.fusion import TARGET_ATR
from app.learning.labels import (
    K_SL,
    K_TP,
    LABEL_VERSION,
    MISSING_SPREAD,
    SLIPPAGE,
    Outcome,
    triple_barrier,
)
from app.market_data.session import XAUUSD_NY_1700

D = Decimal
M5 = timedelta(minutes=5)
T0 = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)  # a Tuesday, market open
ATR = D(4)


def _bar(
    i: int,
    *,
    close: str = "2000",
    high: str | None = None,
    low: str | None = None,
    spread: str | None = "0.20",
    start: datetime = T0,
) -> CandleClosed:
    t = start + i * M5
    c = D(close)
    return CandleClosed(
        symbol="XAUUSD", timeframe=Timeframe.M5, open_time=t, close_time=t + M5, open=c,
        high=D(high) if high else c, low=D(low) if low else c, close=c, volume=None,
        spread=None if spread is None else D(spread), available_at=t + M5,
    )  # fmt: skip


ENTRY = _bar(0)  # bid 2000, spread 0.20: long entry 2000.25, short entry 1999.95


def _label(path: list[CandleClosed], *, entry: CandleClosed = ENTRY, n: int | None = None,
           **kw: object):  # type: ignore[no-untyped-def]  # fmt: skip
    """N defaults to the path's length."""
    return triple_barrier(entry, path, ATR, time_bars=n or len(path), **kw)  # type: ignore[arg-type]


def test_the_label_constants_are_pinned_to_the_label_version() -> None:
    # Changing a barrier constant changes what every label means: bump LABEL_VERSION.
    assert (
        "m07.labels.tb.v1", TARGET_ATR, TARGET_ATR, D("0.05"), D("0.80"),
    ) == (LABEL_VERSION, K_TP, K_SL, SLIPPAGE, MISSING_SPREAD)  # fmt: skip


def test_long_take_profit_first_and_the_short_stops_out() -> None:
    # long TP = 2000.25 + 4 = 2004.25, touched when bid - slippage >= it: high >= 2004.30
    lab = _label([_bar(1, high="2004.30")])
    assert lab is not None and lab.label_version == LABEL_VERSION
    assert lab.long.outcome is Outcome.UP and lab.long.pnl == D(4)
    assert lab.short.outcome is Outcome.UP and lab.short.pnl == D(-4)  # short SL is above
    assert lab.distance == TARGET_ATR * ATR
    assert lab.available_at == _bar(1).available_at


def test_a_bar_touching_both_barriers_is_sl_first() -> None:
    lab = _label([_bar(1, high="2010", low="1990")])
    assert lab is not None
    assert lab.long.outcome is Outcome.DOWN and lab.long.pnl == D(-4)
    assert lab.short.outcome is Outcome.UP and lab.short.pnl == D(-4)


def test_the_long_pays_the_entry_spread() -> None:
    # one cent short of the ask-based TP: without the entry spread it would be a TP
    lab = _label([_bar(1, high="2004.29")])
    assert lab is not None and lab.long.outcome is Outcome.FLAT


def test_the_short_pays_the_exit_bars_spread() -> None:
    # short TP = 1999.95 - 4 = 1995.95, exits at ask: low + bar spread + slippage <= it
    assert _label([_bar(1, low="1995.70")]).short.outcome is Outcome.DOWN  # type: ignore[union-attr]
    # same low, wider spread on the exit bar: the ask never reached the TP
    lab = _label([_bar(1, low="1995.70", spread="0.21")])
    assert lab is not None and lab.short.outcome is Outcome.FLAT


def test_a_missing_spread_costs_the_locked_buffer_never_zero() -> None:
    entry = _bar(0, spread=None)  # long entry = 2000 + 0.80 + 0.05 = 2000.85, TP 2004.85
    lab = _label([_bar(1, high="2004.89")], entry=entry)
    assert lab is not None and lab.long.outcome is Outcome.FLAT
    assert _label([_bar(1, high="2004.90")], entry=entry).long.outcome is Outcome.UP  # type: ignore[union-attr]
    # on the short's exit bar too: low + 0.80 + 0.05 <= 1995.95 needs low <= 1995.10
    assert _label([_bar(1, low="1995.11", spread=None)]).short.outcome is Outcome.FLAT  # type: ignore[union-attr]
    assert _label([_bar(1, low="1995.10", spread=None)]).short.outcome is Outcome.DOWN  # type: ignore[union-attr]


def test_flat_exits_at_the_time_barrier_strictly_inside_the_barriers() -> None:
    path = [_bar(1, close="2001"), _bar(2, close="2002"), _bar(3, close="2003", spread="0.30")]
    lab = _label(path)
    assert lab is not None
    assert lab.long.outcome is Outcome.FLAT and lab.short.outcome is Outcome.FLAT
    assert lab.long.exit == D("2002.95") and lab.short.exit == D("2003.35")
    assert -lab.distance < lab.long.pnl < lab.distance
    assert -lab.distance < lab.short.pnl < lab.distance
    assert lab.available_at == path[-1].available_at == lab.ends_at


def test_bars_after_the_time_barrier_are_never_read() -> None:
    late_tp = [_bar(1), _bar(2), _bar(3), _bar(4, high="2100")]
    lab = _label(late_tp, n=3)
    assert lab is not None and lab.long.outcome is Outcome.FLAT


def test_a_missing_bar_gives_no_label_and_too_few_bars_give_none_yet() -> None:
    assert _label([_bar(1), _bar(3), _bar(4)]) is None
    assert _label([_bar(1), _bar(2)], n=3) is None
    # the short stops out at bar 1 (ask >= 2003.95), but the long still needs its window
    assert _label([_bar(1, high="2004.00"), _bar(3)], n=3) is None


def test_a_scheduled_closure_is_not_a_gap() -> None:
    fri = datetime(2026, 1, 9, 21, 55, tzinfo=UTC)  # Fri 16:55 New York, last bar of week
    sun = datetime(2026, 1, 11, 23, 0, tzinfo=UTC)  # Sun 18:00 New York
    entry = _bar(0, start=fri)
    path = [_bar(i, start=sun) for i in range(3)]
    assert _label(path, entry=entry) is None  # 24/7 assumed: the weekend is a hole
    lab = _label(path, entry=entry, session=XAUUSD_NY_1700)
    assert lab is not None and lab.ends_at == path[-1].close_time


def test_the_ends_at_bound_is_the_last_slot_even_when_resolved_early() -> None:
    lab = _label([_bar(1, high="2010"), _bar(2), _bar(3)])
    assert lab is not None and lab.ends_at == _bar(3).close_time
