"""Outcome labels line up with bar boundaries (#125, PRD #120 slice 0b; MAJ-03).

A snapshot recorded on M5 has `as_of` on any 5-minute boundary, but an M15 or H1 bar opens
only on its own boundary. A label is therefore measured on the finest stored bars that
close exactly at `as_of` and at the horizon end, so M15/H1 horizons are labelled on every
row. A row whose end can never be measured (the data has moved past a hole) is marked once
and not re-queried; a row whose end is merely not due yet is left alone."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.decision import _ctx

from app.core.contracts import CandleClosed, DecisionCreated, FeatureSnapshot
from app.core.enums import DataQuality, DecisionAction, Direction, Mode, Timeframe
from app.learning.bars import BarLog, Horizon, Provenance
from app.learning.experience import ExperienceLog
from app.market_data.store import CandleStore
from app.storage.db import connect, migrate

D = Decimal
M5, M15, H1 = Timeframe.M5, Timeframe.M15, Timeframe.H1
T0 = datetime(2026, 1, 6, 10, 0, tzinfo=UTC)
FIVE = timedelta(minutes=5)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "align.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def candles(conn: sqlite3.Connection) -> CandleStore:
    return CandleStore(conn, dataset_version="v1")


def _close(i: int) -> Decimal:
    return D(2000) + D(i)  # M5 bar i closes at 2000 + i


def _m5_bars(candles: CandleStore, n: int, *, skip: frozenset[int] = frozenset()) -> None:
    for i in range(n):
        if i not in skip:
            _m5_bars_at(candles, i)


def _m5_bars_at(candles: CandleStore, i: int) -> None:
    t = T0 + i * FIVE
    c = _close(i)
    candles.save(CandleClosed(symbol="XAUUSD", timeframe=M5, open_time=t, close_time=t + FIVE,
                              open=c, high=c, low=c, close=c, volume=None,
                              available_at=t + FIVE))  # fmt: skip


def _snapshot(as_of: datetime) -> FeatureSnapshot:
    return FeatureSnapshot(symbol="XAUUSD", feature_schema="m02.features.v2", values={},
                           quality=DataQuality.OK, as_of=as_of, available_at=as_of,
                           valid_until=as_of + FIVE)  # fmt: skip


def _record(log: BarLog, n: int) -> None:
    for i in range(n):
        log.record(_snapshot(T0 + (i + 1) * FIVE), timeframe=M5, provenance=Provenance.BACKFILL,
                   mode=None, dataset_version="v1")  # fmt: skip


def test_m15_and_h1_horizons_recorded_on_m5_are_labelled_on_every_row(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _m5_bars(candles, 40)
    log = BarLog(conn)
    _record(log, 20)  # as_of = close of M5 bars 0..19
    horizons = [Horizon(M15, 2), Horizon(H1, 1)]
    new = log.label_due(candles, symbol="XAUUSD", horizons=horizons, now=T0 + 40 * FIVE)
    by = {(o.horizon, o.as_of): o for o in new}
    assert len([o for o in new if o.horizon is M15]) == 20  # was about 1 in 3 (MAJ-03)
    assert len([o for o in new if o.horizon is H1]) == 20  # was about 1 in 12
    # as_of = close of bar 1 (10:10); M15x2 ends 30 min later = close of bar 7
    assert by[(M15, T0 + 2 * FIVE)].move == _close(7) - _close(1)
    assert by[(H1, T0 + 2 * FIVE)].move == _close(13) - _close(1)


def test_a_row_past_a_hole_is_marked_once_and_a_row_not_yet_due_is_left_alone(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _m5_bars(candles, 30, skip=frozenset({12}))  # bar 12 never arrives
    log = BarLog(conn)
    _record(log, 10)
    h = [Horizon(M15, 1)]  # end = 3 bars after as_of
    now = T0 + 30 * FIVE
    new = log.label_due(candles, symbol="XAUUSD", horizons=h, now=now)
    assert T0 + 10 * FIVE not in {o.as_of for o in new}  # needs bar 12
    assert log.unlabellable("XAUUSD", h[0]) == [T0 + 10 * FIVE]
    # a mark is final and stops the re-query: even a late bar does not reopen the row
    _m5_bars_at(candles, 12)
    assert log.label_due(candles, symbol="XAUUSD", horizons=h, now=now) == []
    # a row whose end bar has not arrived (no data beyond it yet) is not marked, however late
    log.record(_snapshot(T0 + 28 * FIVE), timeframe=M5, provenance=Provenance.BACKFILL,
               mode=None, dataset_version="v1")  # fmt: skip
    log.label_due(candles, symbol="XAUUSD", horizons=h, now=now + timedelta(days=1))
    assert log.unlabellable("XAUUSD", h[0]) == [T0 + 10 * FIVE]


def test_an_experience_with_an_m15_horizon_off_the_m15_grid_is_labelled(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _m5_bars(candles, 20)
    exp_log = ExperienceLog(conn, candles, clock=lambda: T0)
    as_of = T0 + 2 * FIVE  # 10:10: not an M15 boundary
    snap = _ctx().snapshot.model_copy(update={"as_of": as_of})
    d = DecisionCreated(decision_id="dec-1", action=DecisionAction.WAIT, strategy_id="breakout",
                        side=None, expected_value_usd=None, confidence=None,
                        reason_codes=["EV_UNKNOWN"], evidence_refs=["snap"],
                        policy_version="p1")  # fmt: skip
    exp_log.record(decision=d, snapshot=snap, mode=Mode.DEMO, symbol="XAUUSD", cycle_id=None,
                   intent_id=None, direction=Direction.UP, horizon=M15, horizon_bars=1)  # fmt: skip
    (label,) = exp_log.label_due(now=T0 + 20 * FIVE)
    assert label.value == _close(4) - _close(1)
