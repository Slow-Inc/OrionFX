"""M07 news-veto outcome report (#116, PRD #114): for each entry refused with NEWS_VETO,
did the market then move against the refused side (the veto saved a loss) or with it
(the veto missed a gain)? Read-only, point-in-time, over a window."""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.decision import T0, _ctx
from tests.unit.learning.test_experience import _bars, _decision

from app.core.enums import DecisionAction, Direction, Mode, Timeframe, WaitReason
from app.learning.experience import ExperienceLog
from app.learning.veto_report import veto_report
from app.market_data.store import CandleStore
from app.storage.db import connect, migrate

D = Decimal
H1 = Timeframe.H1


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "veto.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def candles(conn: sqlite3.Connection) -> CandleStore:
    return CandleStore(conn, dataset_version="v1")


def _veto(log: ExperienceLog, did: str, side: Direction, hours: int = 0,
          reasons: list[str] | None = None) -> None:  # fmt: skip
    snap = _ctx().snapshot
    snap = snap.model_copy(update={"as_of": T0 + timedelta(hours=hours)})
    d = _decision(DecisionAction.WAIT, did).model_copy(
        update={"reason_codes": reasons or ["NEWS_VETO"]})  # fmt: skip
    log.record(decision=d, snapshot=snap, mode=Mode.DEMO, symbol="XAUUSD", cycle_id=None,
               intent_id=None, direction=side, horizon=H1, horizon_bars=1)  # fmt: skip


def test_vetoes_are_classed_saved_missed_flat_or_unknown(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    log = ExperienceLog(conn, candles, clock=lambda: T0)
    # H1 closes: the bar closing at T0 is 2000; then 1990, 1990, 2005
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "1990"),
          (T0 + timedelta(hours=1), "1990"), (T0 + timedelta(hours=2), "2005"))  # fmt: skip
    _veto(log, "saved", Direction.UP, hours=0)  # refused a BUY; market fell 10: saved
    _veto(log, "flat", Direction.DOWN, hours=1)  # 1990 -> 1990
    _veto(log, "missed", Direction.UP, hours=2)  # refused a BUY; market rose 15: missed
    _veto(log, "saved-down", Direction.DOWN, hours=2)  # refused a SELL; market rose: saved
    _veto(log, "unknown", Direction.UP, hours=3)  # its horizon has not closed
    _veto(log, "outside", Direction.UP, hours=9)  # after the report window
    _veto(log, "other", Direction.UP, hours=0, reasons=["CONFLICT_EVIDENCE"])  # not a veto
    _veto(log, "add", Direction.UP, hours=0, reasons=["NEWS_VETO_ADD"])  # an ADD veto (#121)
    log.label_due(now=T0 + timedelta(hours=5))
    window = {"start": T0 - timedelta(hours=1), "end": T0 + timedelta(hours=4),
              "as_of": T0 + timedelta(hours=5)}  # fmt: skip
    r = veto_report(log, **window)  # type: ignore[arg-type]
    assert (r.vetoes, r.saved, r.missed, r.flat, r.unknown) == (5, 2, 1, 1, 1)
    # ADD vetoes are reported on their own, never mixed into the entry tuning data
    adds = veto_report(log, **window, reason=WaitReason.NEWS_VETO_ADD)  # type: ignore[arg-type]
    assert (adds.vetoes, adds.saved) == (1, 1)
    # in the refused side's favour: BUY -10, SELL 0, BUY +15, SELL -15 -> mean -10 / 4
    assert r.mean_move_for_refused_side == (D(-10) + D(0) + D(15) + D(-15)) / 4


def test_the_report_is_point_in_time(conn: sqlite3.Connection, candles: CandleStore) -> None:
    log = ExperienceLog(conn, candles, clock=lambda: T0)
    _bars(candles, (T0 - timedelta(hours=1), "2000"), (T0, "1990"))
    _veto(log, "v1", Direction.UP)
    log.label_due(now=T0 + timedelta(hours=3))
    before = veto_report(log, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=1),
                         as_of=T0)  # fmt: skip
    after = veto_report(log, start=T0 - timedelta(hours=1), end=T0 + timedelta(hours=1),
                        as_of=T0 + timedelta(hours=3))  # fmt: skip
    assert (before.unknown, before.saved) == (1, 0)  # the label was not yet known
    assert (after.unknown, after.saved) == (0, 1)


def test_no_vetoes_gives_an_empty_report_not_zero_skill(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    log = ExperienceLog(conn, candles, clock=lambda: T0)
    r = veto_report(log, start=T0, end=T0 + timedelta(days=1), as_of=T0 + timedelta(days=1))
    assert r.vetoes == 0 and r.mean_move_for_refused_side is None


def test_vetoes_after_the_report_time_or_at_the_window_end_are_not_counted(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    log = ExperienceLog(conn, candles, clock=lambda: T0)
    _veto(log, "in", Direction.UP, hours=0)
    _veto(log, "later", Direction.UP, hours=2)  # after the report's as_of
    _veto(log, "at-end", Direction.UP, hours=3)  # the window end is exclusive
    window = {"start": T0, "end": T0 + timedelta(hours=3)}
    assert veto_report(log, **window, as_of=T0 + timedelta(hours=1)).vetoes == 1  # type: ignore[arg-type]
    assert veto_report(log, **window, as_of=T0 + timedelta(hours=5)).vetoes == 2  # type: ignore[arg-type]
