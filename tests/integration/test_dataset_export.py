"""M07 dataset slice C (#110, PRD #107): a deterministic, leak-free export of the recorded
bars. Rows are point-in-time (features at as_of, labels known by the window end, the news
assessment known when the features were); splits run in time order and a row whose label
reaches past its split's end is purged; the bytes are hashed and registered as a
DatasetManifest. Exporting twice gives the same bytes."""

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.integration.test_market_recorder import (
    H4,
    LAG,
    M15,
    T0,
    Clock,
    _bar_close,
    _bars,
    _closes,
)

from app.core.contracts import Confidence, NewsAssessment
from app.core.enums import Direction, Mode
from app.core.events import make_event
from app.learning.bars import Provenance
from app.learning.export import export_dataset
from app.learning.lifecycle import DatasetStore, LifecycleRefused, Split
from app.market_data.store import CandleStore
from app.runtime.market_recorder import MarketRecorder
from app.storage.journal import Journal

N = 40


@pytest.fixture
def candles(conn: sqlite3.Connection) -> CandleStore:
    store = CandleStore(conn, dataset_version="v1")
    _bars(store, _closes(N))
    MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   session=None, horizons=[H4], provenance=Provenance.BACKFILL,
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    return store


def _splits() -> list[Split]:
    return [Split(name="train", start=_bar_close(0), end=_bar_close(20)),
            Split(name="validation", start=_bar_close(20), end=_bar_close(30)),
            Split(name="holdout", start=_bar_close(30), end=_bar_close(N))]  # fmt: skip


def _export(conn: sqlite3.Connection, out: Path, version: str = "1", **over: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "symbol": "XAUUSD", "timeframe": M15, "horizons": [H4], "dataset_version": "v1",
        "window_start": _bar_close(0), "window_end": _bar_close(N) + LAG,
        "splits": _splits(), "dataset_id": "xau-m15", "version": version, "out": out,
        "session": None,
    }  # fmt: skip
    args.update(over)
    return export_dataset(conn, **args)  # type: ignore[arg-type]


def _rows(path: Path) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_export_is_deterministic_hashed_and_registered(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    m1 = _export(conn, tmp_path / "a.jsonl")
    m2 = _export(conn, tmp_path / "b.jsonl")
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()
    assert m1 == m2 and m1.content_hash.startswith("sha256:")
    assert DatasetStore(conn).get("xau-m15", "1") == m1
    assert [s.name for s in m1.splits] == ["train", "validation", "holdout"]


def test_rows_whose_label_crosses_their_split_end_are_purged(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    _export(conn, tmp_path / "d.jsonl")
    rows = _rows(tmp_path / "d.jsonl")
    by_split: dict[str, list[str]] = {}
    for r in rows:
        by_split.setdefault(r["split"], []).append(r["as_of"])
    horizon = timedelta(minutes=15 * 4)
    for s in _splits():
        for as_of in by_split[s.name]:
            assert s.start.isoformat() <= as_of
            assert (T0.fromisoformat(as_of) + horizon) <= s.end  # its label ends inside
    # the last 3 bars before each boundary are gone (their 4-bar label crosses it)
    assert _bar_close(18).isoformat() not in by_split["train"]
    assert _bar_close(16).isoformat() in by_split["train"]


def test_every_row_is_labelled_with_what_was_known_by_the_window_end(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    _export(conn, tmp_path / "d.jsonl")
    rows = _rows(tmp_path / "d.jsonl")
    last_labelled = _bar_close(N - 5).isoformat()  # bar N-5 ends with the last stored bar
    for r in rows:
        label = r["labels"]["M15x4"]
        if r["as_of"] > last_labelled:
            assert label is None  # its end bar is beyond the data: unknown, not invented
            continue
        assert label is not None and Decimal(label["move"]) == Decimal(4)
        assert label["available_at"] <= (_bar_close(N) + LAG).isoformat()
        assert r["features"] and r["provenance"] == "BACKFILL" and r["mode"] is None


def test_news_is_joined_as_known_when_the_features_were(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    def news(aid: str, at: object, hours: float, direction: Direction) -> None:
        a = NewsAssessment(
            assessment_id=aid, news_ids=["n1"], source_refs=["https://wire.example/n1"],
            direction=direction, impact=Decimal("0.7"),
            score=Confidence(value=Decimal("0.8"), semantics="test"),
            available_at=at, valid_until=at + timedelta(hours=hours),  # type: ignore[operator]
        )  # fmt: skip
        Journal(conn).append(make_event("news.assessment", source="m03", payload=a,
                                        account_id="local-account", bot_id="bot-1",
                                        mode=Mode.SHADOW, occurred_at=at))  # type: ignore[arg-type]  # fmt: skip

    news("a-early", _bar_close(2) + LAG, 1, Direction.UP)  # live for bars 2..5
    news("a-late", _bar_close(10) + LAG + timedelta(seconds=1), 1, Direction.DOWN)
    _export(conn, tmp_path / "d.jsonl")
    rows = {r["as_of"]: r for r in _rows(tmp_path / "d.jsonl")}
    assert rows[_bar_close(1).isoformat()]["news"] is None  # not yet known
    assert rows[_bar_close(3).isoformat()]["news"]["assessment_id"] == "a-early"
    assert rows[_bar_close(8).isoformat()]["news"] is None  # expired
    # a-late became known 1 s after bar 10's features: bar 10 must not see it
    assert rows[_bar_close(10).isoformat()]["news"] is None
    assert rows[_bar_close(11).isoformat()]["news"]["direction"] == "DOWN"


def test_a_provenance_filter_keeps_only_live_rows(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    m = _export(conn, tmp_path / "d.jsonl", version="live", provenance={Provenance.LIVE})
    assert _rows(tmp_path / "d.jsonl") == []  # this database holds back-fill only
    assert m.content_hash.startswith("sha256:")


def test_a_version_cannot_be_reused_for_different_content(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    _export(conn, tmp_path / "d.jsonl")
    with pytest.raises(LifecycleRefused):
        other = [*_splits()[:2], Split(name="holdout", start=_bar_close(30), end=_bar_close(36))]
        _export(conn, tmp_path / "e.jsonl", splits=other)


def test_a_label_not_yet_known_at_the_window_end_is_left_empty(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    end = _bar_close(36)  # bar 35 closes exactly here and is known 2 s later
    splits = [*_splits()[:2], Split(name="holdout", start=_bar_close(30), end=end)]
    _export(conn, tmp_path / "d.jsonl", version="w", splits=splits, window_end=end)
    rows = {r["as_of"]: r for r in _rows(tmp_path / "d.jsonl")}
    assert rows[_bar_close(31).isoformat()]["labels"]["M15x4"] is not None
    assert rows[_bar_close(32).isoformat()]["labels"]["M15x4"] is None  # known after the end


def test_only_the_requested_candle_dataset_version_is_exported(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    other = CandleStore(conn, dataset_version="v2")
    _bars(other, _closes(N))
    MarketRecorder(conn, candles=other, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   session=None, horizons=[H4], provenance=Provenance.BACKFILL,
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    _export(conn, tmp_path / "d.jsonl")
    assert {r["dataset_version"] for r in _rows(tmp_path / "d.jsonl")} == {"v1"}


def test_only_the_requested_feature_schema_is_exported(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    from app.learning.bars import BarLog

    log = BarLog(conn)
    first = log.snapshots("XAUUSD", M15, start=_bar_close(0), end=_bar_close(1))[0].snapshot
    log.record(first.model_copy(update={"feature_schema": "m02.features.v2"}), timeframe=M15,
               provenance=Provenance.BACKFILL, mode=None, dataset_version="v1")  # fmt: skip
    _export(conn, tmp_path / "d.jsonl")
    rows = _rows(tmp_path / "d.jsonl")
    assert {r["feature_schema"] for r in rows} == {"m02.features.v2"}
    assert len({r["as_of"] for r in rows}) == len(rows)


def test_a_refused_export_leaves_the_registered_file_untouched(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    out = tmp_path / "d.jsonl"
    _export(conn, out)
    before = out.read_bytes()
    other = [*_splits()[:2], Split(name="holdout", start=_bar_close(30), end=_bar_close(36))]
    with pytest.raises(LifecycleRefused):
        _export(conn, out, splits=other)
    assert out.read_bytes() == before
    with pytest.raises(ValueError):  # invalid splits: refused before anything is written
        _export(conn, tmp_path / "bad.jsonl", version="bad", splits=list(reversed(_splits())))
    assert not (tmp_path / "bad.jsonl").exists()


def test_purge_uses_the_longest_horizon_and_rows_say_if_fully_labelled(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    from app.learning.bars import Horizon

    two = Horizon(M15, 2)
    MarketRecorder(conn, candles=candles, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   session=None, horizons=[two, H4], provenance=Provenance.BACKFILL,
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    _export(conn, tmp_path / "d.jsonl", version="two", horizons=[two, H4])
    rows = _rows(tmp_path / "d.jsonl")
    train = [r["as_of"] for r in rows if r["split"] == "train"]
    assert _bar_close(17).isoformat() not in train  # 2-bar label fits, 4-bar label does not
    assert all(r["labelled"] == all(v is not None for v in r["labels"].values()) for r in rows)
    assert any(not r["labelled"] for r in rows)  # the tail of the holdout


def test_news_that_arrived_after_the_close_is_not_joined_to_that_bar(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    """Known 1 s after bar 4 closed (before its snapshot was taken 2 s after): the label
    is measured from that close, so this news must not sit on bar 4's row."""
    at = _bar_close(4) + timedelta(seconds=1)
    a = NewsAssessment(
        assessment_id="a-gap", news_ids=["n1"], source_refs=["https://wire.example/n1"],
        direction=Direction.UP, impact=Decimal("0.7"),
        score=Confidence(value=Decimal("0.8"), semantics="test"),
        available_at=at, valid_until=at + timedelta(hours=1),
    )  # fmt: skip
    Journal(conn).append(make_event("news.assessment", source="m03", payload=a,
                                    account_id="local-account", bot_id="bot-1",
                                    mode=Mode.SHADOW, occurred_at=at))  # fmt: skip
    _export(conn, tmp_path / "d.jsonl")
    rows = {r["as_of"]: r for r in _rows(tmp_path / "d.jsonl")}
    assert rows[_bar_close(4).isoformat()]["news"] is None
    assert rows[_bar_close(5).isoformat()]["news"]["assessment_id"] == "a-gap"
