"""M02 slice D: point-in-time candle store and replay (#32; DAT-04, AT-15).

A stored bar records when it became available; an as-of query returns only what
was known then. Replaying the same ticks gives the same bars and the same
features, and a snapshot built from the store equals one built live."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import Timeframe
from app.market_data.candles import CandleEngine
from app.market_data.features import build_feature_snapshot
from app.market_data.port import RawTick, ReplaySource
from app.market_data.service import ingest
from app.market_data.store import CandleConflict, CandleStore
from app.storage.db import connect, migrate
from app.storage.journal import Journal

D = Decimal
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
LATER = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "md.db")
    migrate(c)
    yield c
    c.close()


def _ticks(minutes: int = 600) -> list[RawTick]:
    """One tick every 20 s with a deterministic zig-zag drift, broker time UTC+2."""
    out = []
    price = D("2000.00")
    for i in range(minutes * 3):
        price += D("0.13") if (i // 37) % 3 else D("-0.11")
        t = T0 + timedelta(seconds=20 * i)
        local = (t + timedelta(hours=2)).replace(tzinfo=None)
        out.append(RawTick("XAUUSD.m", local, 120, price, price + D("0.20")))
    return out


def _engine() -> CandleEngine:
    return CandleEngine("XAUUSD", {"XAUUSD.m": "XAUUSD"}, clock=lambda: LATER)


def _run(conn: sqlite3.Connection, ticks: list[RawTick]) -> CandleStore:
    store = CandleStore(conn, dataset_version="replay-v1")
    ingest(ReplaySource(ticks), _engine(), store, journal=Journal(conn))
    return store


def test_as_of_returns_only_what_was_known(conn: sqlite3.Connection) -> None:
    store = _run(conn, _ticks(120))
    bars = store.bars("XAUUSD", Timeframe.M5, as_of=LATER)
    mid = bars[10]
    known = store.bars("XAUUSD", Timeframe.M5, as_of=mid.available_at)
    assert known[-1] == mid and all(b.available_at <= mid.available_at for b in known)
    just_before = store.bars(
        "XAUUSD", Timeframe.M5, as_of=mid.available_at - timedelta(microseconds=1)
    )
    assert just_before[-1] == bars[9]


def test_ingest_resumes_from_its_cursor_without_duplicates(conn: sqlite3.Connection) -> None:
    ticks = _ticks(120)
    store = CandleStore(conn, dataset_version="replay-v1")
    engine = _engine()
    src = ReplaySource(ticks)
    first = ingest(src, engine, store, journal=Journal(conn), limit=100)
    ingest(src, engine, store, journal=Journal(conn), cursor=first, limit=10_000)
    again = CandleStore(conn, dataset_version="replay-v1")
    assert len(again.bars("XAUUSD", Timeframe.M5, as_of=LATER)) == 23  # 120 min -> 23 closed
    events = Journal(conn).recent(["candle.closed"], limit=1000)
    assert len(events) == len({e.envelope.event_id for e in events})


def test_a_stored_bar_cannot_be_rewritten(conn: sqlite3.Connection) -> None:
    store = _run(conn, _ticks(30))
    bar = store.bars("XAUUSD", Timeframe.M5, as_of=LATER)[0]
    store.save(bar)  # identical: a no-op
    changed = bar.model_copy(update={"close": bar.close + 1, "high": bar.high + 1})
    with pytest.raises(CandleConflict):
        store.save(changed)


def test_dataset_versions_are_separate(conn: sqlite3.Connection) -> None:
    _run(conn, _ticks(30))
    other = CandleStore(conn, dataset_version="replay-v2")
    assert other.bars("XAUUSD", Timeframe.M5, as_of=LATER) == []


def test_replay_is_deterministic_and_matches_live_features(tmp_path: Path) -> None:
    """AT-15: same events -> same bars and features; no future information."""
    ticks = _ticks(600)
    snaps = []
    for run in ("a", "b"):
        c = connect(tmp_path / f"{run}.db")
        migrate(c)
        store = _run(c, ticks)
        now = store.bars("XAUUSD", Timeframe.M5, as_of=LATER)[80].available_at
        by_tf = {tf: store.bars("XAUUSD", tf, as_of=now) for tf in Timeframe}
        snaps.append(build_feature_snapshot("XAUUSD", by_tf, now))
        c.close()
    assert snaps[0] == snaps[1]

    # The same snapshot computed live, straight from the engine, without the store.
    live = _engine()
    bars = [b.event for t in ticks for b in live.on_tick(t)]
    now = snaps[0].available_at
    by_tf = {tf: [b for b in bars if b.timeframe is tf] for tf in Timeframe}
    assert build_feature_snapshot("XAUUSD", by_tf, now) == snaps[0]


def test_candle_events_are_journaled(conn: sqlite3.Connection) -> None:
    _run(conn, _ticks(30))
    events = Journal(conn).recent(["candle.closed"], limit=100)
    assert events and all(e.envelope.source == "M02" for e in events)


def test_replay_republishes_nothing_twice_and_repairs_a_lost_event(
    conn: sqlite3.Connection,
) -> None:
    ticks = _ticks(60)
    store = CandleStore(conn, dataset_version="replay-v1")
    # A first run stored its bars but crashed before publishing any event.
    eng = _engine()
    for t in ticks:
        for closed in eng.on_tick(t):
            store.save(closed.event)
    assert Journal(conn).recent(["candle.closed"], limit=1000) == []
    for _ in range(2):  # the replay, twice
        ingest(ReplaySource(ticks), _engine(), store, journal=Journal(conn))
    events = Journal(conn).recent(["candle.closed"], limit=1000)
    stored = sum(len(store.bars("XAUUSD", tf, as_of=LATER)) for tf in Timeframe)
    assert len(events) == stored
