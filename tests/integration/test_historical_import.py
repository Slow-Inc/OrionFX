"""Historical M1 bar import (#128, PRD #120 slice 1). Imported M1 bars are validated like
live data (UTC, order, duplicates, gaps), resampled to M5/M15/H1 on the data source's
session schedule, and stored in resumable, idempotent chunks. Each candle dataset version
belongs to exactly one data source, and `audit` reports bars in closed time and real gaps.
All data here is synthetic."""

import gzip
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import Timeframe
from app.market_data.candles import Issue
from app.market_data.history import CsvM1Source, import_history
from app.market_data.port import M1Bar
from app.market_data.session import XAUUSD_NY_1700, SessionSchedule
from app.market_data.sources import SourceConflict, SourceRegistry, UnknownSource
from app.market_data.store import CandleStore
from app.runtime.market_recorder import MarketRecorder

D = Decimal
M1 = timedelta(minutes=1)
H1 = timedelta(hours=1)
# Wednesday 2026-01-07: New York is UTC-5, so the daily break is 22:00-23:00 UTC.
WED = datetime(2026, 1, 7, tzinfo=UTC)
LATER = datetime(2030, 1, 1, tzinfo=UTC)


class ListSource:
    """A historical source over a fixed list, in the order given."""

    def __init__(self, bars: list[M1Bar]) -> None:
        self.bars = bars
        self.calls: list[tuple[datetime, datetime]] = []

    def m1_bars(self, start: datetime, end: datetime) -> Iterator[M1Bar]:
        self.calls.append((start, end))
        return iter([b for b in self.bars if start <= b.open_time < end])


def _bar(t: datetime, price: str = "2000", spread: str | None = "0.20") -> M1Bar:
    p = D(price)
    return M1Bar(open_time=t, open=p, high=p + D("0.5"), low=p - D("0.5"), close=p + D("0.1"),
                 volume=D(10), spread=None if spread is None else D(spread))  # fmt: skip


def _minutes(start: datetime, end: datetime, *, skip: tuple[datetime, datetime] | None = None,
             closed: bool = False) -> list[M1Bar]:  # fmt: skip
    """One bar per minute in [start, end); the daily break is left out unless `closed`."""
    out, t = [], start
    while t < end:
        in_break = WED.replace(hour=22) <= t < WED.replace(hour=23)
        in_skip = skip is not None and skip[0] <= t < skip[1]
        if (closed or not in_break) and not in_skip:
            out.append(_bar(t, str(2000 + (t - start) // M1)))
        t += M1
    return out


def _import(conn: sqlite3.Connection, source: object, **over: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "data_source": "dukascopy", "symbol": "XAUUSD", "dataset_version": "duka-v1",
        "start": WED.replace(hour=20), "end": WED + timedelta(days=1),
        "chunk": H1, "clock": lambda: LATER,
    }  # fmt: skip
    args.update(over)
    return import_history(conn, source, **args)  # type: ignore[arg-type]


def _stored(conn: sqlite3.Connection, tf: Timeframe) -> list:  # type: ignore[type-arg]
    return CandleStore(conn, dataset_version="duka-v1").bars("XAUUSD", tf, as_of=LATER)


def test_m1_bars_resample_to_live_shaped_bars_available_at_their_close(
    conn: sqlite3.Connection,
) -> None:
    _import(conn, ListSource(_minutes(WED.replace(hour=20), WED.replace(hour=21))),
            end=WED.replace(hour=21))  # fmt: skip
    m5 = _stored(conn, Timeframe.M5)
    assert len(m5) == 12 and len(_stored(conn, Timeframe.M15)) == 4
    (h1,) = _stored(conn, Timeframe.H1)
    first = m5[0]
    assert (first.open, first.close) == (D(2000), D("2004.1"))
    assert (first.high, first.low) == (D("2004.5"), D("1999.5"))
    assert first.volume == D(50) and first.spread == D("0.20")
    assert first.available_at == first.close_time == WED.replace(hour=20, minute=5)
    assert h1.open == D(2000) and h1.close == D("2059.1") and h1.quality_flags == []


def test_the_scheduled_break_is_not_a_gap_and_the_audit_fits(conn: sqlite3.Connection) -> None:
    report = _import(conn, ListSource(_minutes(WED.replace(hour=20), WED + timedelta(days=1))))
    after = [b for b in _stored(conn, Timeframe.M5) if b.open_time == WED.replace(hour=23)]
    assert after and after[0].quality_flags == []
    assert all(b.quality_flags == [] for tf in Timeframe for b in _stored(conn, tf))
    assert report.fits
    for tf in Timeframe:
        assert report.in_closed(tf) == 0 and report.unexpected_gaps(tf) == 0


def test_a_hole_while_open_flags_the_bars_around_it_and_is_reported(
    conn: sqlite3.Connection,
) -> None:
    hole = (WED.replace(hour=20, minute=30), WED.replace(hour=20, minute=35))
    report = _import(conn, ListSource(_minutes(WED.replace(hour=20), WED.replace(hour=21),
                                               skip=hole)),
                     end=WED.replace(hour=21))  # fmt: skip
    flagged = {(b.timeframe, b.open_time.minute) for tf in Timeframe
               for b in _stored(conn, tf) if b.quality_flags == ["GAP"]}  # fmt: skip
    assert flagged == {(Timeframe.M5, 35), (Timeframe.M15, 30), (Timeframe.H1, 0)}
    assert report.unexpected_gaps(Timeframe.M5) == 1 and report.fits


def test_a_hole_on_a_chunk_boundary_flags_the_first_bar_of_the_next_chunk(
    conn: sqlite3.Connection,
) -> None:
    hole = (WED.replace(hour=20, minute=55), WED.replace(hour=21))
    _import(conn, ListSource(_minutes(WED.replace(hour=20), WED.replace(hour=22), skip=hole)),
            end=WED.replace(hour=22))  # fmt: skip
    m5 = {b.open_time: b for b in _stored(conn, Timeframe.M5)}
    assert m5[WED.replace(hour=21)].quality_flags == ["GAP"]
    h1 = {b.open_time: b for b in _stored(conn, Timeframe.H1)}
    assert h1[WED.replace(hour=20)].quality_flags == ["GAP"]  # the hole is inside it
    assert h1[WED.replace(hour=21)].quality_flags == []


def test_a_bar_in_closed_time_is_flagged_by_the_audit(conn: sqlite3.Connection) -> None:
    bars = _minutes(WED.replace(hour=21), WED.replace(hour=23, minute=5), closed=True)
    report = _import(conn, ListSource(bars), start=WED.replace(hour=21),
                     end=WED + timedelta(days=1))  # fmt: skip
    assert not report.fits
    assert report.in_closed(Timeframe.M5) == 12 and report.in_closed(Timeframe.H1) == 1
    assert report.unexpected_gaps(Timeframe.M5) == 0


def test_bad_rows_are_dropped_and_counted_like_live_ticks(conn: sqlite3.Connection) -> None:
    t = WED.replace(hour=20)
    bars = _minutes(t, t + timedelta(minutes=10))
    crossed = M1Bar(open_time=t + 3 * M1, open=D(1), high=D(0), low=D(2), close=D(1),
                    volume=None, spread=None)  # fmt: skip
    future = _bar(LATER + H1)
    source = ListSource([*bars[:5], bars[4], bars[2], crossed, *bars[5:], future])
    report = _import(conn, source, end=LATER + 2 * H1, chunk=timedelta(days=4000))
    issues = report.issues
    assert issues[Issue.DUPLICATE] == 1 and issues[Issue.OUT_OF_ORDER] == 1
    assert issues[Issue.INVALID_QUOTE] == 1 and issues[Issue.FUTURE_TICK] == 1
    assert sum(b.volume for b in _stored(conn, Timeframe.M5)) == D(100)  # type: ignore[misc]


def test_an_invalid_bar_is_dropped(conn: sqlite3.Connection) -> None:
    t = WED.replace(hour=20)
    crossed = M1Bar(open_time=t, open=D(1), high=D(0), low=D(2), close=D(1),
                    volume=None, spread=None)  # fmt: skip
    off_grid = _bar(t + timedelta(seconds=30))
    negative = _bar(t + M1, spread="-0.1")
    report = _import(conn, ListSource([crossed, off_grid, negative, _bar(t + 2 * M1)]),
                     end=WED.replace(hour=21))  # fmt: skip
    assert report.issues[Issue.INVALID_QUOTE] == 3
    assert len(_stored(conn, Timeframe.M5)) == 1


def test_rerunning_a_chunk_is_idempotent_whatever_the_clock(conn: sqlite3.Connection) -> None:
    # A hole on the chunk edge: the 21:00 bar's GAP flag depends on the bar before the chunk.
    hole = (WED.replace(hour=20, minute=55), WED.replace(hour=21))
    source = ListSource(_minutes(WED.replace(hour=20), WED.replace(hour=22), skip=hole))
    _import(conn, source, end=WED.replace(hour=22))
    before = conn.execute("SELECT * FROM candles ORDER BY 1, 2, 3, 4").fetchall()
    # Re-run every chunk later (the resume would skip them): same bars, no conflict.
    for hour in (20, 21):
        _import(conn, source, start=WED.replace(hour=hour), end=WED.replace(hour=hour) + H1,
                clock=lambda: LATER + timedelta(days=9))  # fmt: skip
    assert conn.execute("SELECT * FROM candles ORDER BY 1, 2, 3, 4").fetchall() == before


def test_an_interrupted_import_resumes_from_the_last_stored_chunk(
    conn: sqlite3.Connection,
) -> None:
    source = ListSource(_minutes(WED.replace(hour=18), WED.replace(hour=22)))
    _import(conn, source, start=WED.replace(hour=18), end=WED.replace(hour=20))
    source.calls.clear()
    report = _import(conn, source, start=WED.replace(hour=18), end=WED.replace(hour=22))
    # 19:00-20:00 holds the newest stored bar and is re-run; earlier chunks are skipped.
    # The source is read once, from the resume point, and split at chunk ends.
    assert source.calls == [(WED.replace(hour=19), WED.replace(hour=22))]
    assert [c.start.hour for c in report.chunks] == [19, 20, 21]
    assert len(_stored(conn, Timeframe.H1)) == 4


def test_chunks_must_line_up_with_h1_bars(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="H1"):
        _import(conn, ListSource([]), chunk=timedelta(minutes=30))
    with pytest.raises(ValueError, match="H1"):
        _import(conn, ListSource([]), start=WED.replace(hour=20, minute=5))


def test_a_dataset_version_belongs_to_one_data_source(conn: sqlite3.Connection) -> None:
    _import(conn, ListSource([]))
    registry = SourceRegistry(conn)
    assert registry.source_of("duka-v1") == "dukascopy"
    assert registry.schedule_of("duka-v1").schedule_id == "xauusd-ny-1700"
    with pytest.raises(SourceConflict):
        _import(conn, ListSource([]), data_source="mt5")
    with pytest.raises(UnknownSource):
        _import(conn, ListSource([]), data_source="somewhere", dataset_version="x")
    assert registry.source_of("x") is None
    with pytest.raises(UnknownSource):
        registry.schedule_of("x")


# --- CSV reader -------------------------------------------------------------------------

HEADER = "open_time,open,high,low,close,volume,spread\n"


def test_the_csv_reader_reads_the_documented_format(tmp_path: Path) -> None:
    path = tmp_path / "xau.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(HEADER)
        f.write("2026-01-07T20:00:00Z,2000.1,2000.5,1999.9,2000.2,12,0.25\n")
        f.write("2026-01-07T15:01:00-05:00,2000.2,2000.6,2000.0,2000.3,,\n")
        f.write("2026-01-07T20:02:00+00:00,2000.3,2000.7,2000.1,2000.4,3,0.30\n")
    bars = list(CsvM1Source(path).m1_bars(WED.replace(hour=20), WED.replace(hour=20, minute=2)))
    assert [b.open_time for b in bars] == [WED.replace(hour=20), WED.replace(hour=20, minute=1)]
    assert bars[0].spread == D("0.25") and bars[0].volume == D(12)
    assert bars[1].spread is None and bars[1].volume is None
    assert bars[1].open_time.tzinfo == UTC


def test_the_csv_reader_refuses_naive_times_and_unknown_headers(tmp_path: Path) -> None:
    naive = tmp_path / "naive.csv"
    naive.write_text(HEADER + "2026-01-07T20:00:00,1,1,1,1,1,0.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="UTC offset"):
        list(CsvM1Source(naive).m1_bars(WED, LATER))
    other = tmp_path / "other.csv"
    other.write_text("time,o,h,l,c\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        list(CsvM1Source(other).m1_bars(WED, LATER))
    bad = tmp_path / "bad.csv"
    bad.write_text(HEADER + "2026-01-07T20:00:00Z,abc,1,1,1,1,0.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        list(CsvM1Source(bad).m1_bars(WED, LATER))


def test_a_csv_file_imports_end_to_end(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "xau.csv"
    lines = [HEADER]
    for b in _minutes(WED.replace(hour=20), WED.replace(hour=21)):
        t = b.open_time.astimezone(timezone(timedelta(hours=2))).isoformat()
        lines.append(f"{t},{b.open},{b.high},{b.low},{b.close},{b.volume},{b.spread}\n")
    path.write_text("".join(lines), encoding="utf-8")
    report = _import(conn, CsvM1Source(path), end=WED.replace(hour=21))
    assert len(_stored(conn, Timeframe.H1)) == 1 and report.fits


def test_a_recorder_over_an_imported_version_uses_its_sources_schedule(
    conn: sqlite3.Connection,
) -> None:
    _import(conn, ListSource([]))
    store = CandleStore(conn, dataset_version="duka-v1")

    def recorder(session: SessionSchedule | None) -> MarketRecorder:
        return MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=Timeframe.M5,
                              context_timeframes=[], horizons=[], session=session)  # fmt: skip

    assert recorder(None)._session is XAUUSD_NY_1700
    assert recorder(XAUUSD_NY_1700)._session is XAUUSD_NY_1700
    other = SessionSchedule("other", "Europe/Zurich", time(22, 0), time(23, 0))
    with pytest.raises(ValueError, match="xauusd-ny-1700"):
        recorder(other)
    unbound = CandleStore(conn, dataset_version="live-v1")
    assert MarketRecorder(conn, candles=unbound, symbol="XAUUSD", timeframe=Timeframe.M5,
                          context_timeframes=[], horizons=[])._session is None  # fmt: skip
