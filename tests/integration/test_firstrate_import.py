"""FirstRate gold futures (GC) 1-minute import (#128, PRD #120 slice 1). The format and
rules come from FirstRate's public pages (`docs/research/2026-11-28-xauusd-data-sources.md`):
naive US Eastern bar-start times, no-trade minutes omitted, the ratio-adjusted continuous
series, CME Globex hours. Every fixture is a tiny hand-made CSV: no real data."""

import gzip
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tests.support.seal import Opened

from app.core.enums import QualityFlag, Timeframe
from app.learning.bars import Horizon, Provenance
from app.market_data.firstrate import FirstRateGcSource
from app.market_data.history import ImportReport, import_history
from app.market_data.rolls import Roll, RollConflict, RollLog, read_rolls
from app.market_data.session import CME_GLOBEX_GC
from app.market_data.sources import NO_TRADE_FILL, SourceRegistry, UnknownSource
from app.market_data.store import CandleConflict, CandleStore
from app.market_data.techniques.indicators import make_library, observe
from app.runtime.market_recorder import MarketRecorder

D = Decimal
ET = ZoneInfo("America/New_York")
M1 = timedelta(minutes=1)
H1 = timedelta(hours=1)
LATER = datetime(2030, 1, 1, tzinfo=UTC)
NAME = "GC_full_1min_continuous_ratio_adjusted.txt"


OPENED = Opened()
"""The fixtures lie in protocol v1's sealed holdout, so recording them needs an opening."""
# Wednesday 2026-01-07: New York is UTC-5 (EST), so the CME break is 22:00-23:00 UTC.
WED = datetime(2026, 1, 7, tzinfo=UTC)


def _et(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ET).astimezone(UTC)


def _row(local: datetime, price: Decimal, volume: str = "7") -> str:
    """One bar in FirstRate's documented format: naive Eastern bar-start time, OHLCV."""
    stamp = local.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp},{price},{price + D('0.3')},{price - D('0.2')},{price + D('0.1')},{volume}"


def _write(path: Path, starts: list[datetime], *, header: bool = False) -> Path:
    lines = ["timestamp,open,high,low,close,volume"] if header else []
    lines += [_row(t, D(2000) + i) for i, t in enumerate(starts)]
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _minutes(
    start: datetime, end: datetime, *, skip: Sequence[tuple[datetime, datetime]] = ()
) -> list[datetime]:
    """Every open-session minute in [start, end), except the skipped (no-trade) ranges."""
    out, t = [], start
    while t < end:
        if CME_GLOBEX_GC.is_open(t) and not any(a <= t < b for a, b in skip):
            out.append(t)
        t += M1
    return out


def _import(conn: sqlite3.Connection, path: Path, **over: object) -> ImportReport:
    args: dict[str, object] = {
        "data_source": "firstrate-gc", "symbol": "XAUUSD", "dataset_version": "frd-gc-v1",
        "start": WED.replace(hour=20), "end": WED + timedelta(days=1),
        "chunk": H1, "clock": lambda: LATER,
    }  # fmt: skip
    args.update(over)
    return import_history(conn, FirstRateGcSource(path), **args)  # type: ignore[arg-type]


def _stored(conn: sqlite3.Connection, tf: Timeframe, version: str = "frd-gc-v1") -> list:  # type: ignore[type-arg]
    return CandleStore(conn, dataset_version=version).bars("XAUUSD", tf, as_of=LATER)


# --- the parser ----------------------------------------------------------------------------


def test_eastern_bar_start_times_convert_to_utc_across_dst(tmp_path: Path) -> None:
    path = tmp_path / NAME
    path.write_text("2026-01-07 15:00:00,2000.1,2000.5,1999.9,2000.2,12\n"
                    "2026-07-08 15:00:00,2400.1,2400.5,2399.9,2400.2,3.5\n",
                    encoding="utf-8")  # fmt: skip
    winter, summer = FirstRateGcSource(path).m1_bars(WED, LATER)
    assert winter.open_time == datetime(2026, 1, 7, 20, tzinfo=UTC)  # EST, UTC-5
    assert summer.open_time == datetime(2026, 7, 8, 19, tzinfo=UTC)  # EDT, UTC-4
    assert (winter.open, winter.high, winter.low, winter.close) == (
        D("2000.1"), D("2000.5"), D("1999.9"), D("2000.2"))  # fmt: skip
    assert winter.volume == D(12) and summer.volume == D("3.5")  # contracts; decimals allowed
    assert winter.spread is None  # trades only: FirstRate has no spread


def test_a_header_line_is_optional(tmp_path: Path) -> None:
    """FirstRate's pages disagree on whether files carry a header; both forms are read."""
    starts = _minutes(WED.replace(hour=20), WED.replace(hour=20, minute=3))
    plain = _write(tmp_path / NAME, starts)
    (tmp_path / "h").mkdir()
    headed = _write(tmp_path / "h" / NAME, starts, header=True)
    assert list(FirstRateGcSource(plain).m1_bars(WED, LATER)) == list(
        FirstRateGcSource(headed).m1_bars(WED, LATER)) and len(starts) == 3  # fmt: skip


def test_the_requested_range_is_honoured_and_gzip_is_read(tmp_path: Path) -> None:
    path = tmp_path / (NAME + ".gz")
    starts = _minutes(WED.replace(hour=20), WED.replace(hour=20, minute=5))
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write("".join(_row(t, D(2000)) + "\n" for t in starts))
    bars = list(FirstRateGcSource(path).m1_bars(starts[1], starts[3]))
    assert [b.open_time for b in bars] == starts[1:3]


@pytest.mark.parametrize("local", ["2026-11-01 01:30:00", "2026-03-08 02:30:00"])
def test_an_ambiguous_or_missing_eastern_time_refuses_the_file(tmp_path: Path, local: str) -> None:
    """Both US clock changes fall on a Sunday when Globex is closed, so such a time means
    the file is not what we think it is. It is refused, never guessed."""
    path = tmp_path / NAME
    path.write_text(f"{local},1,1,1,1,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        list(FirstRateGcSource(path).m1_bars(WED, LATER))


@pytest.mark.parametrize(
    "line",
    ["2026-01-07 15:00:00,1,1,1,1",  # a column short
     "2026-01-07T15:00:00Z,1,1,1,1,1",  # not FirstRate's datetime format
     "2026-01-07 15:00:00,abc,1,1,1,1",
     "timestamp,open,high,low,close,volume"],  # a header only ever comes first
)  # fmt: skip
def test_a_malformed_row_refuses_the_file(tmp_path: Path, line: str) -> None:
    path = tmp_path / NAME
    path.write_text("2026-01-07 14:59:00,1,1,1,1,1\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        list(FirstRateGcSource(path).m1_bars(WED, LATER))


@pytest.mark.parametrize(
    "name",
    ["GC_full_1min_continuous_UNadjusted.txt", "GC_full_1min_continuous_absolute_adjusted.txt",
     "GC_full_5min_continuous_ratio_adjusted.txt", "GCZ26_1min.txt"],
)  # fmt: skip
def test_only_the_ratio_adjusted_1min_continuous_series_is_accepted(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(ValueError, match="ratio_adjusted"):
        FirstRateGcSource(tmp_path / name)


# --- the session schedule and the data source ---------------------------------------------


def test_firstrate_gc_is_a_data_source_on_the_cme_globex_schedule(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _import(conn, _write(tmp_path / NAME, []))
    row = conn.execute("SELECT data_source, schedule_id FROM candle_sources"
                       " WHERE dataset_version = 'frd-gc-v1'").fetchone()  # fmt: skip
    assert tuple(row) == ("firstrate-gc", "cme-globex-gc-et")
    assert SourceRegistry(conn).schedule_of("frd-gc-v1") is CME_GLOBEX_GC
    assert NO_TRADE_FILL["firstrate-gc"] == timedelta(minutes=15)


def test_a_day_of_bars_fits_the_cme_schedule(conn: sqlite3.Connection, tmp_path: Path) -> None:
    starts = _minutes(WED.replace(hour=20), WED + timedelta(days=1))
    report = _import(conn, _write(tmp_path / NAME, starts))
    assert report.fits and report.filled_minutes == 0
    assert all(report.unexpected_gaps(tf) == 0 for tf in Timeframe)
    assert all(b.quality_flags == [] for tf in Timeframe for b in _stored(conn, tf))


# --- the no-trade-minute rule --------------------------------------------------------------


def test_a_short_no_trade_run_is_filled_flat_and_is_not_a_gap(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """15 open minutes with no trade (15:28-15:42 ET): flat bars at the previous close,
    volume 0. The two M5 bars made only of them carry NO_TRADE; nothing is a GAP."""
    quiet = (_et("2026-01-07 15:28"), _et("2026-01-07 15:43"))
    starts = _minutes(WED.replace(hour=20), WED.replace(hour=21), skip=[quiet])
    report = _import(conn, _write(tmp_path / NAME, starts), end=WED.replace(hour=21))
    assert report.filled_runs == [15] and report.filled_minutes == 15
    assert all(report.unexpected_gaps(tf) == 0 for tf in Timeframe)
    m5 = {b.open_time: b for b in _stored(conn, Timeframe.M5)}
    assert len(m5) == 12
    last_close = D(2000) + 27 + D("0.1")  # the 15:27 bar's close
    for t in (_et("2026-01-07 15:30"), _et("2026-01-07 15:35")):
        bar = m5[t]
        assert bar.quality_flags == [QualityFlag.NO_TRADE.value]
        assert bar.open == bar.high == bar.low == bar.close == last_close
        assert bar.volume == D(0) and bar.spread is None
    assert m5[_et("2026-01-07 15:25")].quality_flags == []  # it holds real trades
    assert m5[_et("2026-01-07 15:40")].quality_flags == []
    assert all(QualityFlag.GAP.value not in b.quality_flags
               for tf in Timeframe for b in _stored(conn, tf))  # fmt: skip


def test_a_run_longer_than_the_limit_is_a_real_gap(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    hole = (_et("2026-01-07 15:28"), _et("2026-01-07 15:44"))  # 16 minutes
    starts = _minutes(WED.replace(hour=20), WED.replace(hour=21), skip=[hole])
    report = _import(conn, _write(tmp_path / NAME, starts), end=WED.replace(hour=21))
    assert report.filled_runs == [] and report.unexpected_gaps(Timeframe.M5) == 2
    m5 = {b.open_time: b for b in _stored(conn, Timeframe.M5)}
    assert _et("2026-01-07 15:30") not in m5
    assert m5[_et("2026-01-07 15:40")].quality_flags == [QualityFlag.GAP.value]


def test_a_run_never_spans_a_scheduled_closure(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """Last trade 16:54 ET, first after the break 18:03 ET. The 3 open minutes after the
    reopen are a run and are filled. The 5 before the close have no trade after them in
    their session: filling them would look past the break, so they stay a real gap."""
    quiet = [(_et("2026-01-07 16:55"), _et("2026-01-07 17:00")),
             (_et("2026-01-07 18:00"), _et("2026-01-07 18:03"))]  # fmt: skip
    starts = _minutes(WED.replace(hour=21), WED + timedelta(days=1), skip=quiet)
    report = _import(conn, _write(tmp_path / NAME, starts), start=WED.replace(hour=21))
    assert report.filled_runs == [3] and report.fits
    assert report.unexpected_gaps(Timeframe.M5) == 1
    m5 = {b.open_time: b for b in _stored(conn, Timeframe.M5)}
    assert _et("2026-01-07 16:55") not in m5
    assert m5[_et("2026-01-07 18:00")].quality_flags == [QualityFlag.GAP.value]
    assert not any(_et("2026-01-07 17:00") <= t < _et("2026-01-07 18:00") for t in m5)


def test_a_run_after_a_closure_at_the_range_start_is_filled_as_one_pass(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The range starts at the 18:00 ET reopen, with no trade until 18:03. The run is
    seeded by the 16:59 bar, over an hour earlier: the MAX_CLOSED lead reads it."""
    quiet = (_et("2026-01-07 18:00"), _et("2026-01-07 18:03"))
    path = _write(tmp_path / NAME, _minutes(WED.replace(hour=21), WED + timedelta(days=1),
                                            skip=[quiet]))  # fmt: skip
    _import(conn, path, start=WED.replace(hour=21), dataset_version="one-pass")
    report = _import(conn, path, start=WED.replace(hour=23))
    assert report.filled_runs == [3] and report.unexpected_gaps(Timeframe.M5) == 0
    m5 = _stored(conn, Timeframe.M5)
    assert m5[0].open_time == _et("2026-01-07 18:00") and m5[0].quality_flags == []
    assert m5 == [b for b in _stored(conn, Timeframe.M5, "one-pass")
                  if b.open_time >= WED.replace(hour=23)]  # fmt: skip


@pytest.mark.parametrize("drop", ["invalid", "out_of_order"])
def test_a_run_holding_a_dropped_row_is_a_real_gap(
    conn: sqlite3.Connection, tmp_path: Path, drop: str
) -> None:
    """A row dropped by validation in 15:28-15:42 ET means the minute had data that was
    bad, not that it had no trade: the run is never filled."""
    quiet = (_et("2026-01-07 15:28"), _et("2026-01-07 15:43"))
    path = _write(tmp_path / NAME, _minutes(WED.replace(hour=20), WED.replace(hour=21),
                                            skip=[quiet]))  # fmt: skip
    lines = path.read_text(encoding="utf-8").splitlines()
    after = next(i for i, x in enumerate(lines) if x.startswith("2026-01-07 15:43"))
    if drop == "invalid":
        lines.insert(after, "2026-01-07 15:35:00,1,0,2,1,1")  # crossed: high below low
    else:
        lines.insert(after + 1, _row(_et("2026-01-07 15:35"), D(2000)))  # after 15:43
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = _import(conn, path, end=WED.replace(hour=21))
    assert sum(report.issues.values()) == 1 and report.filled_runs == []
    m5 = {b.open_time: b for b in _stored(conn, Timeframe.M5)}
    assert _et("2026-01-07 15:30") not in m5 and _et("2026-01-07 15:35") not in m5
    assert m5[_et("2026-01-07 15:40")].quality_flags == [QualityFlag.GAP.value]


def test_a_run_with_no_trade_after_it_in_the_range_is_not_filled(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Without a later trade, nothing says trading resumed: the tail stays missing."""
    starts = _minutes(WED.replace(hour=20), _et("2026-01-07 15:50"))
    report = _import(conn, _write(tmp_path / NAME, starts), end=WED.replace(hour=21))
    assert report.filled_runs == []
    assert max(b.open_time for b in _stored(conn, Timeframe.M5)) == _et("2026-01-07 15:45")


def test_extending_a_range_that_ended_inside_a_no_trade_run_is_refused(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The first import ends inside the 15:50-15:59 run, so its tail is a gap. A later,
    longer range would fill it and rebuild a stored bar differently: the store refuses
    (fail-closed). Import a new dataset version instead."""
    quiet = (_et("2026-01-07 15:50"), _et("2026-01-07 16:00"))
    path = _write(tmp_path / NAME, _minutes(WED.replace(hour=20), WED.replace(hour=22),
                                            skip=[quiet]))  # fmt: skip
    _import(conn, path, end=WED.replace(hour=21))
    (h1,) = _stored(conn, Timeframe.H1)
    assert h1.quality_flags == [QualityFlag.GAP.value]
    with pytest.raises(CandleConflict):
        _import(conn, path, end=WED.replace(hour=22))


def test_filled_bars_are_labelled_and_keep_technique_windows_whole(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The point of the rule: a horizon ending on a NO_TRADE bar is labelled (no
    MISSING_BAR), and a window over it has no GAP_IN_WINDOW. A 16-minute hole is the
    control: it stays a gap for both. The hole lies before the newest H1 bar (the label
    frontier), and the ATR window (16 M5 bars) ends at 15:20 ET so that it spans it."""
    for minutes, version in ((15, "short"), (16, "long")):
        quiet = (_et("2026-01-07 14:28"), _et("2026-01-07 14:28") + minutes * M1)
        path = _write(tmp_path / f"{version}_{NAME}", _minutes(
            WED.replace(hour=19), WED.replace(hour=21), skip=[quiet]))  # fmt: skip
        _import(conn, path, start=WED.replace(hour=19), end=WED.replace(hour=21),
                dataset_version=version)  # fmt: skip
        candles = CandleStore(conn, dataset_version=version)
        MarketRecorder(conn, candles=candles, symbol="XAUUSD", timeframe=Timeframe.M5,
                       context_timeframes=[], horizons=[Horizon(Timeframe.M5, 1)],
                       provenance=Provenance.BACKFILL, clock=lambda: WED + timedelta(days=1),
                       session=None, opening=OPENED).tick()  # fmt: skip  # sealed holdout
        sql = "SELECT COUNT(*) FROM learning_unlabellable WHERE dataset_version = ?"
        unlabellable = conn.execute(sql, (version,)).fetchone()[0]
        bars = [b for b in candles.bars("XAUUSD", Timeframe.M5, as_of=LATER)
                if b.open_time <= _et("2026-01-07 15:20")]  # fmt: skip
        atr = make_library(atr_n=3)["atr_3"]
        obs = observe(atr, bars, bars[-1].available_at, session=CME_GLOBEX_GC)
        gap_in_window = QualityFlag.GAP_IN_WINDOW in obs.quality_flags
        if version == "short":
            assert unlabellable == 0 and not gap_in_window and "atr" in obs.values
        else:
            assert unlabellable > 0 and gap_in_window


def test_rows_before_the_range_only_seed_the_fill(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The 15:59 ET bar seeds the 16:00-16:02 run; it is neither stored nor counted, and
    nor is a bad row before the range."""
    quiet = (_et("2026-01-07 16:00"), _et("2026-01-07 16:03"))
    starts = _minutes(WED.replace(hour=20), WED.replace(hour=22), skip=[quiet])
    path = _write(tmp_path / NAME, starts)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[30] = "2026-01-07 15:30:00,1,0,2,1,1"  # crossed: high below low
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = _import(conn, path, start=WED.replace(hour=21), end=WED.replace(hour=22))
    assert report.filled_runs == [3] and not report.issues
    m5 = _stored(conn, Timeframe.M5)
    assert m5[0].open_time == WED.replace(hour=21) and len(m5) == 12
    assert m5[0].open == D(2000) + 59 + D("0.1")  # flat at the 15:59 close
    assert m5[0].quality_flags == []  # 16:03 and 16:04 traded


def test_a_resumed_import_fills_exactly_as_one_pass(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A no-trade run across a chunk edge (15:56-16:04 ET) needs the bar before the chunk.
    Re-running from the newest stored chunk must rebuild identical bars, not a GAP."""
    quiet = (_et("2026-01-07 15:56"), _et("2026-01-07 16:05"))
    path = _write(tmp_path / NAME, _minutes(WED.replace(hour=20), WED.replace(hour=22),
                                            skip=[quiet]))  # fmt: skip
    _import(conn, path, end=WED.replace(hour=22), dataset_version="one-pass")
    _import(conn, path, end=WED.replace(hour=22))
    again = _import(conn, path, end=WED.replace(hour=22))  # resumes at the 21:00 UTC chunk
    assert [c.start for c in again.chunks] == [WED.replace(hour=21)]
    assert again.filled_runs == [9]  # counted where it lies, in the re-run chunk
    for tf in Timeframe:
        assert _stored(conn, tf) == _stored(conn, tf, "one-pass")
    assert _stored(conn, Timeframe.M5)[12].quality_flags == [QualityFlag.NO_TRADE.value]


# --- roll dates ----------------------------------------------------------------------------

ROLL = Roll(roll_at=_et("2026-01-26 00:00"), from_contract="GCG26", to_contract="GCJ26")


def test_roll_dates_are_recorded_per_dataset_version_append_only(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    log = RollLog(conn)
    with pytest.raises(UnknownSource):
        log.record("frd-gc-v1", [ROLL])  # a version needs its data source first
    _import(conn, _write(tmp_path / NAME, []))
    later = Roll(_et("2026-03-26 00:00"), "GCJ26", "GCM26")
    log.record("frd-gc-v1", [later, ROLL])
    log.record("frd-gc-v1", [ROLL])  # the same roll again is a no-op
    assert log.rolls_of("frd-gc-v1") == [ROLL, later]
    with pytest.raises(RollConflict):
        log.record("frd-gc-v1", [Roll(ROLL.roll_at, "GCG26", "GCM26")])
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM candle_rolls")
    conn.execute("INSERT OR REPLACE INTO candle_rolls VALUES"
                 " ('frd-gc-v1', ?, 'GCG26', 'GCM26')", (ROLL.roll_at.isoformat(),))  # fmt: skip
    assert log.rolls_of("frd-gc-v1") == [ROLL, later]
    assert log.rolls_of("other") == []


def test_the_roll_csv_needs_utc_offsets(tmp_path: Path) -> None:
    good = tmp_path / "rolls.csv"
    good.write_text("roll_at,from_contract,to_contract\n"
                    "2026-01-26T00:00:00-05:00,GCG26,GCJ26\n", encoding="utf-8")  # fmt: skip
    assert read_rolls(good) == [ROLL]
    for bad_line in ("2026-01-26T00:00:00,GCG26,GCJ26", "2026-01-26T05:00:00Z,GCG26,GCG26"):
        bad = tmp_path / "bad.csv"
        bad.write_text("roll_at,from_contract,to_contract\n" + bad_line + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="line 2"):
            read_rolls(bad)
