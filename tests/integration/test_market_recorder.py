"""M07 dataset from real operation, slice A (#108, PRD #107): every closed bar's features
are recorded from exactly the bars available at its close, tagged LIVE or BACKFILL, and
labelled later with the raw close-to-close move over each horizon, stamped with when that
move became known. A gap gives no label. Recording is idempotent and never raises."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import UserConfig
from app.core.contracts import CandleClosed
from app.core.enums import Mode, Timeframe
from app.learning.bars import BarLog, Horizon, Provenance
from app.market_data.store import CandleStore
from app.runtime.market_recorder import MarketRecorder
from app.storage.config_store import ConfigStore

D = Decimal
M15 = Timeframe.M15
T0 = datetime(2027, 3, 1, 0, 0, tzinfo=UTC)
STEP = timedelta(minutes=15)
LAG = timedelta(seconds=2)
H4 = Horizon(M15, 4)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def _bars(candles: CandleStore, closes: list[str], *, skip: set[int] = frozenset()) -> None:  # type: ignore[assignment]
    for i, c in enumerate(closes):
        if i in skip:
            continue
        t, p = T0 + i * STEP, D(c)
        candles.save(CandleClosed(
            symbol="XAUUSD", timeframe=M15, open_time=t, close_time=t + STEP, open=p - D(3),
            high=p + D("0.5"), low=p - D("3.5"), close=p, volume=D(100), spread=D("0.20"),
            available_at=t + STEP + LAG,
        ))  # fmt: skip


def _closes(n: int) -> list[str]:
    return [str(2000 + i) for i in range(n)]


def _recorder(conn: sqlite3.Connection, candles: CandleStore, clock: Clock,
              provenance: Provenance = Provenance.LIVE) -> MarketRecorder:  # fmt: skip
    return MarketRecorder(conn, session=None, candles=candles, symbol="XAUUSD", timeframe=M15,
                          context_timeframes=[], horizons=[H4], provenance=provenance,
                          clock=clock)  # fmt: skip


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT * FROM {table} ORDER BY as_of").fetchall()
    finally:
        conn.row_factory = None


def _bar_close(i: int) -> datetime:
    return T0 + (i + 1) * STEP


@pytest.fixture
def candles(conn: sqlite3.Connection) -> CandleStore:
    return CandleStore(conn, dataset_version="v1")


def test_each_available_bar_is_recorded_once_from_what_was_known_at_its_close(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _bars(candles, _closes(10))
    clock = Clock(_bar_close(5) + LAG)  # bars 0..5 are available, 6.. are not yet
    rec = _recorder(conn, candles, clock)
    rec.tick()
    rows = _rows(conn, "learning_snapshots")
    assert [r["as_of"] for r in rows] == [_bar_close(i).isoformat() for i in range(6)]
    assert all(r["available_at"] == (datetime.fromisoformat(r["as_of"]) + LAG).isoformat()
               for r in rows)  # fmt: skip
    rec.tick()  # idempotent
    assert len(_rows(conn, "learning_snapshots")) == 6
    clock.t = _bar_close(9) + LAG
    rec.tick()  # catches up
    assert len(_rows(conn, "learning_snapshots")) == 10


def test_live_rows_carry_the_account_mode_and_backfill_rows_say_so(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    v, cfg = ConfigStore(conn).current()
    cfg = UserConfig.model_validate(
        cfg.model_dump() | {"trading": cfg.trading.model_dump() | {"account_mode": "SHADOW"}}
    )
    ConfigStore(conn).update(cfg, expected_version=v, actor="test")
    _bars(candles, _closes(3))
    _recorder(conn, candles, Clock(_bar_close(0) + LAG)).tick()
    _recorder(conn, candles, Clock(_bar_close(2) + LAG), Provenance.BACKFILL).tick()
    rows = _rows(conn, "learning_snapshots")
    assert (rows[0]["provenance"], rows[0]["mode"]) == ("LIVE", Mode.SHADOW.value)
    assert {(r["provenance"], r["mode"]) for r in rows[1:]} == {("BACKFILL", None)}
    assert {r["dataset_version"] for r in rows} == {"v1"}
    assert {r["feature_schema"] for r in rows} == {"m02.features.v2"}


def test_a_label_is_the_raw_move_known_only_once_the_horizon_bar_closed(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _bars(candles, _closes(10))
    clock = Clock(_bar_close(4) + LAG)
    rec = _recorder(conn, candles, clock)
    rec.tick()  # bar 0's horizon ends with bar 4, just available
    (label,) = _rows(conn, "learning_outcomes")
    assert label["as_of"] == _bar_close(0).isoformat()
    assert (label["horizon"], label["horizon_bars"]) == ("M15", 4)
    assert D(label["move"]) == D(4) and D(label["spread"]) == D("0.20")
    assert label["available_at"] == (_bar_close(4) + LAG).isoformat()
    clock.t = _bar_close(9) + LAG
    rec.tick()
    assert len(_rows(conn, "learning_outcomes")) == 6  # bars 0..5; 6..9 are not due


def test_a_gap_gives_no_label_never_a_stretched_one(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _bars(candles, _closes(12), skip={5})
    _recorder(conn, candles, Clock(_bar_close(11) + LAG)).tick()
    labelled = {r["as_of"] for r in _rows(conn, "learning_outcomes")}
    assert _bar_close(0).isoformat() in labelled  # ends at bar 4
    assert _bar_close(1).isoformat() not in labelled  # its end bar 5 is missing
    assert _bar_close(4).isoformat() in labelled  # bars 4 -> 8 both exist: exactly 4 bars
    assert _bar_close(5).isoformat() not in labelled  # bar 5 itself never existed


def test_labels_and_snapshots_are_append_only(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _bars(candles, _closes(6))
    _recorder(conn, candles, Clock(_bar_close(5) + LAG)).tick()
    for sql in (
        "UPDATE learning_outcomes SET move = '0'",
        "DELETE FROM learning_outcomes",
        "UPDATE learning_snapshots SET body = '{}'",
        "DELETE FROM learning_snapshots",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)


def test_a_failure_is_reported_and_backed_off_not_raised(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    class Broken(CandleStore):
        def bars(self, *a: object, **k: object) -> list[CandleClosed]:
            raise sqlite3.OperationalError("disk I/O error")

    clock = Clock(_bar_close(3))
    rec = _recorder(conn, Broken(conn, dataset_version="v1"), clock)
    rec.tick()  # must not raise
    from app.storage.journal import Journal

    assert Journal(conn).recent(["health.updated"])


def test_the_bar_log_exposes_rows_for_export(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _bars(candles, _closes(8))
    _recorder(conn, candles, Clock(_bar_close(7) + LAG)).tick()
    log = BarLog(conn)
    snaps = log.snapshots("XAUUSD", M15, start=T0, end=_bar_close(7) + LAG)
    outs = log.outcomes("XAUUSD", H4, start=T0, end=_bar_close(7) + LAG)
    assert len(snaps) == 8 and len(outs) == 4
    assert snaps[0].snapshot.as_of == _bar_close(0) and outs[0].move == D(4)


def test_each_snapshot_equals_one_built_from_only_the_bars_known_at_that_close(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    """Long history (more bars than any technique looks back): an early bar's snapshot
    must still see its own past, not the newest window."""
    from app.market_data.features import build_feature_snapshot
    from app.runtime.market_recorder import LOOKBACK

    n = LOOKBACK + 30
    _bars(candles, _closes(n))
    _recorder(conn, candles, Clock(_bar_close(n - 1) + LAG)).tick()
    snaps = BarLog(conn).snapshots("XAUUSD", M15, start=T0, end=_bar_close(n) + LAG)
    for i in (LOOKBACK + 2, n - 1):
        at = _bar_close(i) + LAG
        known = candles.bars("XAUUSD", M15, as_of=at, limit=LOOKBACK)
        expected = build_feature_snapshot("XAUUSD", {M15: known}, at)
        assert snaps[i].snapshot == expected


def test_a_backfill_long_after_the_data_still_labels_every_row(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _bars(candles, _closes(12))
    later = Clock(_bar_close(11) + timedelta(days=30))
    _recorder(conn, candles, later, Provenance.BACKFILL).tick()
    assert len(_rows(conn, "learning_outcomes")) == 8  # bars 0..7 (8..11 lack 4 more bars)


def test_rows_caught_up_after_downtime_are_tagged_backfill_not_live(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    _bars(candles, _closes(10))
    _recorder(conn, candles, Clock(_bar_close(9) + LAG)).tick()  # bot was down until bar 9
    rows = {r["as_of"]: r["provenance"] for r in _rows(conn, "learning_snapshots")}
    assert rows[_bar_close(9).isoformat()] == "LIVE"  # the bar it saw as it happened
    assert rows[_bar_close(0).isoformat()] == "BACKFILL"  # bars it never saw live


def test_health_returns_to_ok_after_a_failure_clears(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    from app.core.contracts import HealthUpdated
    from app.storage.journal import Journal, payload_as

    class Flaky(CandleStore):
        fail = True

        def bars(self, *a: object, **k: object) -> list[CandleClosed]:  # type: ignore[override]
            if Flaky.fail:
                raise sqlite3.OperationalError("disk I/O error")
            return super().bars(*a, **k)  # type: ignore[arg-type]

    _bars(candles, _closes(4))
    clock = Clock(_bar_close(3) + LAG)
    rec = _recorder(conn, Flaky(conn, dataset_version="v1"), clock)
    rec.tick()
    Flaky.fail = False
    clock.t += timedelta(minutes=3)
    rec.tick()
    rec.tick()  # a second good pass reports nothing new
    statuses = [payload_as(e, HealthUpdated).status.value
                for e in reversed(Journal(conn).recent(["health.updated"]))]  # fmt: skip
    assert statuses == ["DEGRADED", "OK"]


def test_a_horizon_that_does_not_line_up_with_the_recorded_bars_is_refused(
    conn: sqlite3.Connection, candles: CandleStore
) -> None:
    with pytest.raises(ValueError, match="horizon"):
        MarketRecorder(conn, session=None, candles=candles, symbol="XAUUSD", timeframe=Timeframe.H1,
                       context_timeframes=[], horizons=[Horizon(M15, 4)],
                       clock=Clock(T0))  # fmt: skip


def test_bars_after_a_cursor_skip_the_history_already_seen(candles: CandleStore) -> None:
    _bars(candles, _closes(6))
    later = candles.bars("XAUUSD", M15, as_of=_bar_close(5) + LAG, after=_bar_close(2) + LAG)
    assert [b.close_time for b in later] == [_bar_close(3), _bar_close(4), _bar_close(5)]


def test_the_recorder_has_no_default_session_schedule() -> None:
    """Train/serve parity (#129): whoever wires the recorder chooses the schedule, as the
    export and label scoring must; a forgotten one is an error, not a silent 24/7."""
    import inspect

    param = inspect.signature(MarketRecorder).parameters["session"]
    assert param.default is inspect.Parameter.empty
