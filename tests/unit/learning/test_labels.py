"""Triple-barrier labels m07.labels.tb.v2 (#129; research backlog item 0). One label per
bar, the same for both sides: barriers at the entry bid close +/- TARGET_ATR x ATR_M15 on
the bid path. UP when the upper barrier is touched first, DOWN when the lower is, FLAT
when neither is within N expected session slots. No cost is inside the label. A bar
touching both barriers is AMBIGUOUS: no class label, counted so its rate can be
reported. Labels are read on the finest stored timeframe that can measure them."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.bet import LABEL_VERSION, TARGET_ATR
from app.core.contracts import CandleClosed
from app.core.enums import Timeframe
from app.learning.bars import Horizon
from app.learning.labels import K, LabelStats, Outcome, label_at, triple_barrier
from app.market_data.session import XAUUSD_NY_1700
from app.market_data.store import CandleStore
from app.storage.db import connect, migrate

D = Decimal
M5, M15 = Timeframe.M5, Timeframe.M15
STEP = {M5: timedelta(minutes=5), M15: timedelta(minutes=15)}
T0 = datetime(2026, 1, 6, 15, 0, tzinfo=UTC)  # a Tuesday, market open
ATR = D(4)  # upper barrier 2004, lower 1996


def _bar(
    i: int,
    *,
    close: str = "2000",
    high: str | None = None,
    low: str | None = None,
    start: datetime = T0,
    tf: Timeframe = M5,
) -> CandleClosed:
    t = start + i * STEP[tf]
    c = D(close)
    return CandleClosed(
        symbol="XAUUSD", timeframe=tf, open_time=t, close_time=t + STEP[tf], open=c,
        high=D(high) if high else max(c, D(2000)), low=D(low) if low else min(c, D(2000)),
        close=c, volume=None, spread=None, available_at=t + STEP[tf],
    )  # fmt: skip


ENTRY = _bar(0)


def _label(path: list[CandleClosed], *, n: int | None = None, **kw: object):  # type: ignore[no-untyped-def]
    """N defaults to the path's length."""
    return triple_barrier(ENTRY if "entry" not in kw else kw.pop("entry"), path, ATR,  # type: ignore[arg-type]
                          time_bars=n or len(path), **kw)  # type: ignore[arg-type]  # fmt: skip


def test_the_label_constants_are_pinned_to_the_label_version() -> None:
    # Changing a barrier constant changes what every label means: bump LABEL_VERSION.
    assert (LABEL_VERSION, K) == ("m07.labels.tb.v2", TARGET_ATR)


def test_the_upper_barrier_touched_first_is_up() -> None:
    lab = _label([_bar(1, high="2003.99"), _bar(2, high="2004")])
    assert lab is not None and lab.label_version == LABEL_VERSION
    assert lab.outcome is Outcome.UP and lab.move == D(4) and lab.distance == TARGET_ATR * ATR
    assert lab.available_at == _bar(2).available_at


def test_the_lower_barrier_touched_first_is_down() -> None:
    lab = _label([_bar(1, low="1996.01"), _bar(2, low="1996"), _bar(3, high="2010")])
    assert lab is not None and lab.outcome is Outcome.DOWN and lab.move == D(-4)


def test_a_bar_touching_both_barriers_is_ambiguous_not_a_class() -> None:
    lab = _label([_bar(1, high="2004", low="1996")])
    assert lab is not None and lab.outcome is Outcome.AMBIGUOUS
    assert not lab.labelled and lab.move is None
    one_side_first = _label([_bar(1, high="2004"), _bar(2, high="2010", low="1990")], n=2)
    assert one_side_first is not None and one_side_first.outcome is Outcome.UP


def test_flat_at_the_time_barrier_with_its_drift() -> None:
    path = [_bar(1, close="2001"), _bar(2, close="2002"), _bar(3, close="2003.5")]
    lab = _label(path)
    assert lab is not None and lab.outcome is Outcome.FLAT and lab.labelled
    assert lab.move == D("3.5") and lab.available_at == path[-1].available_at == lab.ends_at
    assert _label([*path, _bar(4, high="2100")], n=3).outcome is Outcome.FLAT  # type: ignore[union-attr]


def test_a_missing_bar_gives_no_label_and_too_few_bars_give_none_yet() -> None:
    assert _label([_bar(1), _bar(3), _bar(4)]) is None
    assert _label([_bar(1), _bar(2)], n=3) is None
    assert _label([_bar(1, high="2004"), _bar(3)], n=3) is not None  # resolved before it


def test_a_scheduled_closure_is_not_a_gap() -> None:
    fri = datetime(2026, 1, 9, 21, 55, tzinfo=UTC)  # Fri 16:55 New York, last bar of week
    sun = datetime(2026, 1, 11, 23, 0, tzinfo=UTC)  # Sun 18:00 New York
    entry = _bar(0, start=fri)
    path = [_bar(i, start=sun) for i in range(3)]
    assert _label(path, entry=entry) is None  # 24/7 assumed: the weekend is a hole
    lab = _label(path, entry=entry, session=XAUUSD_NY_1700)
    assert lab is not None and lab.ends_at == path[-1].close_time


def test_the_label_does_not_depend_on_spread_or_the_price_level() -> None:
    path = [_bar(1, high="2002"), _bar(2, low="1995")]
    base = _label(path)
    shifted = [b.model_copy(update={k: getattr(b, k) + 1000 for k in ("open", "high", "low",
                                                                       "close")})
               for b in [ENTRY, *path]]  # fmt: skip
    lab = _label(shifted[1:], entry=shifted[0])
    assert base is not None and lab is not None
    assert (lab.outcome, lab.move) == (base.outcome, base.move) == (Outcome.DOWN, D(-4))
    wide = [b.model_copy(update={"spread": D(5)}) for b in path]
    assert _label(wide) == base


# --- from the store: the finest timeframe, point in time ------------------------------------


@pytest.fixture
def candles(tmp_path: Path) -> Iterator[CandleStore]:
    c = connect(tmp_path / "labels.db")
    migrate(c)
    yield CandleStore(c, dataset_version="v1")
    c.close()


def _save(store: CandleStore, bars: list[CandleClosed]) -> None:
    for b in bars:
        store.save(b)


def test_the_finest_stored_timeframe_resolves_what_a_coarser_bar_cannot(
    candles: CandleStore,
) -> None:
    # one M15 bar spans both barriers; its M5 bars show the upper one came first
    _save(candles, [_bar(0, tf=M15), _bar(1, tf=M15, high="2005", low="1995")])
    h = Horizon(M15, 1)
    as_of = T0 + STEP[M15]
    now = as_of + 2 * STEP[M15]
    lab = label_at(candles, "XAUUSD", as_of, ATR, h, now=now)
    assert lab is not None and lab.outcome is Outcome.AMBIGUOUS and lab.timeframe is M15
    _save(candles, [_bar(2), _bar(3, high="2005"), _bar(4), _bar(5, low="1995")])
    lab = label_at(candles, "XAUUSD", as_of, ATR, h, now=now)
    assert lab is not None and lab.outcome is Outcome.UP and lab.timeframe is M5


def test_a_label_reads_only_bars_known_at_now(candles: CandleStore) -> None:
    _save(candles, [_bar(0), _bar(1), _bar(2, high="2004"), _bar(3)])
    as_of, h = T0 + STEP[M5], Horizon(M15, 1)
    assert label_at(candles, "XAUUSD", as_of, ATR, h, now=_bar(1).available_at) is None
    lab = label_at(candles, "XAUUSD", as_of, ATR, h, now=_bar(2).available_at)
    assert lab is not None and lab.outcome is Outcome.UP
    assert label_at(candles, "XAUUSD", as_of, None, h, now=_bar(3).available_at) is None


def test_label_stats_report_the_dual_touch_rate_and_the_flat_share() -> None:
    def lab(path: list[CandleClosed]):  # type: ignore[no-untyped-def]
        return _label(path, n=1)

    labels = [lab([_bar(1, high="2004")]), lab([_bar(1)]), lab([_bar(1)]),
              lab([_bar(1, high="2004", low="1996")]), None]  # fmt: skip
    stats = LabelStats.of(labels)
    assert (stats.rows, stats.missing, stats.ambiguous, stats.up, stats.down, stats.flat) == (
        5, 1, 1, 1, 0, 2)  # fmt: skip
    assert stats.dual_touch_rate == D(1) / D(4) and stats.flat_share == D(2) / D(3)
    assert LabelStats.of([]).dual_touch_rate is None
