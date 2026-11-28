"""M07 dataset export (#110, PRD #107; #128, PRD #120): a deterministic, leak-free export
of the recorded bars. Rows are point-in-time (features at as_of, labels known by the
window end, the news assessment known at the bar's close). Folds come from the X02
walk-forward splitter, with an embargo and a sealed holdout. A row belongs to a window
only if its label ends inside it, and every row names its data source. The bytes are
hashed and registered as a DatasetManifest that records the fold plan and the data
sources. Exporting twice gives the same bytes."""

import json
import sqlite3
from collections.abc import Collection
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.integration.test_market_recorder import (
    H4,
    LAG,
    M15,
    STEP,
    Clock,
    _bar_close,
    _bars,
    _closes,
)

from app.core.contracts import Confidence, NewsAssessment
from app.core.enums import Direction, Mode
from app.core.events import make_event
from app.learning.bars import BarLog, Horizon, Provenance
from app.learning.export import export_dataset
from app.learning.lifecycle import DatasetStore, FoldWindow, LifecycleRefused
from app.market_data.sources import SourceRegistry, UnknownSource
from app.market_data.store import CandleStore
from app.runtime.market_recorder import MarketRecorder
from app.storage.journal import Journal
from app.validation.splits import walk_forward

N = 40
LONGEST = H4.length  # one hour: four M15 bars
PLAN = {"train": 12 * STEP, "test": 6 * STEP, "embargo": 4 * STEP, "holdout": 8 * STEP}


def _record(conn: sqlite3.Connection, version: str, *, skip: Collection[int] = ()) -> CandleStore:
    store = CandleStore(conn, dataset_version=version)
    _bars(store, _closes(N), skip=set(skip))
    MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[H4], provenance=Provenance.BACKFILL,
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    return store


@pytest.fixture
def candles(conn: sqlite3.Connection) -> CandleStore:
    SourceRegistry(conn).register("v1", "dukascopy")
    return _record(conn, "v1")


def _export(conn: sqlite3.Connection, out: Path, version: str = "1", **over: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "symbol": "XAUUSD", "timeframe": M15, "horizons": [H4], "dataset_versions": ["v1"],
        "window_start": _bar_close(0), "window_end": _bar_close(N),
        "dataset_id": "xau-m15", "version": version, "out": out, **PLAN,
    }  # fmt: skip
    args.update(over)
    return export_dataset(conn, **args)  # type: ignore[arg-type]


def _rows(path: Path) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_export_is_deterministic_hashed_and_registered_with_its_fold_plan(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    m1 = _export(conn, tmp_path / "a.jsonl")
    m2 = _export(conn, tmp_path / "b.jsonl")
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()
    assert m1 == m2 and m1.content_hash.startswith("sha256:")
    assert DatasetStore(conn).get("xau-m15", "1") == m1
    plan = walk_forward(_bar_close(0), _bar_close(N), **PLAN)
    assert [FoldWindow(**asdict(f)) for f in plan.folds] == m1.folds
    assert len(m1.folds) == 2 and m1.embargo == PLAN["embargo"]
    assert [(s.name, s.start, s.end) for s in m1.splits] == [
        ("development", _bar_close(0), plan.holdout.start),
        ("holdout", plan.holdout.start, plan.holdout.end),
    ]
    assert m1.data_sources == {"v1": "dukascopy"}
    assert {r["data_source"] for r in _rows(tmp_path / "a.jsonl")} == {"dukascopy"}


def test_every_window_is_purged_by_label_end_and_availability(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    # A snapshot known long after its bar: its label ends inside fold 0's training window,
    # but it became available only after that window ended, so it belongs to no window.
    (early,) = BarLog(conn).snapshots("XAUUSD", M15, start=_bar_close(4),
                                      end=_bar_close(4) + STEP)  # fmt: skip
    late = early.snapshot.model_copy(update={"available_at": _bar_close(13),
                                             "valid_until": _bar_close(14)})  # fmt: skip
    assert late.available_at > late.as_of + LONGEST
    SourceRegistry(conn).register("late-v1", "dukascopy")
    BarLog(conn).record(late, timeframe=M15, provenance=Provenance.BACKFILL, mode=None,
                        dataset_version="late-v1")  # fmt: skip
    m = _export(conn, tmp_path / "d.jsonl", dataset_versions=["v1", "late-v1"])
    rows = _rows(tmp_path / "d.jsonl")
    assert not [r for r in rows if r["dataset_version"] == "late-v1"]
    rows = [r for r in rows if r["dataset_version"] == "v1"]
    holdout = m.splits[1]

    def inside(r: dict, start: datetime, end: datetime) -> bool:  # type: ignore[type-arg]
        as_of = _t(r["as_of"])
        return start <= as_of and as_of + LONGEST <= end and _t(r["available_at"]) <= end

    for r in rows:
        for k in r["folds"]["train"]:
            assert inside(r, m.folds[k].train_start, m.folds[k].train_end)
        for k in r["folds"]["test"]:
            assert inside(r, m.folds[k].test_start, m.folds[k].test_end)
        if r["split"] == "holdout":
            assert inside(r, holdout.start, holdout.end) and r["folds"] == {"train": [], "test": []}
        else:
            assert r["folds"]["train"] or r["folds"]["test"]
    trains0 = {r["as_of"] for r in rows if 0 in r["folds"]["train"]}
    # fold 0 trains on [bar_close(0), bar_close(12)): its last 3 bars' labels cross the end
    assert _bar_close(8).isoformat() in trains0 and _bar_close(9).isoformat() not in trains0
    tests0 = sorted(r["as_of"] for r in rows if 0 in r["folds"]["test"])
    assert tests0 == [_bar_close(i).isoformat() for i in (16, 17, 18)]
    held = sorted(r["as_of"] for r in rows if r["split"] == "holdout")
    assert held == [_bar_close(i).isoformat() for i in range(32, 37)]
    # rows in the embargo before the holdout belong to nothing and are left out
    assert not {_bar_close(i).isoformat() for i in range(28, 32)} & {r["as_of"] for r in rows}


def test_no_fold_tests_before_its_training_labels_end(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    m = _export(conn, tmp_path / "d.jsonl")
    rows = _rows(tmp_path / "d.jsonl")
    for k, fold in enumerate(m.folds):
        train_end = max(_t(r["as_of"]) + LONGEST for r in rows if k in r["folds"]["train"])
        first_test = min(_t(r["as_of"]) for r in rows if k in r["folds"]["test"])
        assert train_end + m.embargo <= first_test
        assert fold.train_end + m.embargo <= fold.test_start
    last_label = max(_t(r["as_of"]) + LONGEST for r in rows if r["split"] == "development")
    assert last_label + m.embargo <= m.splits[1].start  # the holdout embargo


def test_test_windows_come_after_training_data_from_every_source(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    """A second feed covering a different span: one fold plan on one time axis."""
    SourceRegistry(conn).register("mt5-v1", "mt5")
    _record(conn, "mt5-v1", skip=set(range(10)))
    m = _export(conn, tmp_path / "d.jsonl", dataset_versions=["v1", "mt5-v1"])
    rows = _rows(tmp_path / "d.jsonl")
    assert m.data_sources == {"v1": "dukascopy", "mt5-v1": "mt5"}
    assert {r["data_source"] for r in rows} == {"dukascopy", "mt5"}
    for k in range(len(m.folds)):
        train = [r for r in rows if k in r["folds"]["train"]]
        test = [r for r in rows if k in r["folds"]["test"]]
        assert {r["data_source"] for r in test} == {"dukascopy", "mt5"}
        latest_label = max(_t(r["as_of"]) + LONGEST for r in train)
        assert all(latest_label + m.embargo <= _t(r["as_of"]) for r in test)


def test_an_embargo_shorter_than_the_longest_label_is_refused(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="embargo"):
        _export(conn, tmp_path / "d.jsonl", embargo=3 * STEP)
    with pytest.raises(ValueError, match="holdout"):
        _export(conn, tmp_path / "d.jsonl", holdout=timedelta(0))
    assert not (tmp_path / "d.jsonl").exists()


def test_a_dataset_version_with_no_data_source_is_refused(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    _record(conn, "unknown-v1")
    with pytest.raises(UnknownSource):
        _export(conn, tmp_path / "d.jsonl", dataset_versions=["v1", "unknown-v1"])


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
        assert label["available_at"] <= _bar_close(N).isoformat()
        assert r["features"] and r["provenance"] == "BACKFILL" and r["mode"] is None


def _news(conn: sqlite3.Connection, aid: str, at: datetime, hours: float, d: Direction) -> None:
    a = NewsAssessment(
        assessment_id=aid, news_ids=["n1"], source_refs=["https://wire.example/n1"],
        direction=d, impact=Decimal("0.7"),
        score=Confidence(value=Decimal("0.8"), semantics="test"),
        available_at=at, valid_until=at + timedelta(hours=hours),
    )  # fmt: skip
    Journal(conn).append(make_event("news.assessment", source="m03", payload=a,
                                    account_id="local-account", bot_id="bot-1",
                                    mode=Mode.SHADOW, occurred_at=at))  # fmt: skip


def test_news_is_joined_as_known_when_the_features_were(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    _news(conn, "a-early", _bar_close(2) + LAG, 1, Direction.UP)  # live for bars 2..5
    _news(conn, "a-late", _bar_close(5) + LAG + timedelta(seconds=1), 1, Direction.DOWN)
    _export(conn, tmp_path / "d.jsonl")
    rows = {r["as_of"]: r for r in _rows(tmp_path / "d.jsonl")}
    assert rows[_bar_close(1).isoformat()]["news"] is None  # not yet known
    assert rows[_bar_close(3).isoformat()]["news"]["assessment_id"] == "a-early"
    # a-late became known 1 s after bar 5's features: bar 5 must not see it
    assert rows[_bar_close(5).isoformat()]["news"]["assessment_id"] == "a-early"
    assert rows[_bar_close(6).isoformat()]["news"]["direction"] == "DOWN"
    assert rows[_bar_close(16).isoformat()]["news"] is None  # expired


def test_news_that_arrived_after_the_close_is_not_joined_to_that_bar(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    """Known 1 s after bar 4 closed (before its snapshot was taken 2 s after): the label
    is measured from that close, so this news must not sit on bar 4's row."""
    _news(conn, "a-gap", _bar_close(4) + timedelta(seconds=1), 1, Direction.UP)
    _export(conn, tmp_path / "d.jsonl")
    rows = {r["as_of"]: r for r in _rows(tmp_path / "d.jsonl")}
    assert rows[_bar_close(4).isoformat()]["news"] is None
    assert rows[_bar_close(5).isoformat()]["news"]["assessment_id"] == "a-gap"


def test_a_provenance_filter_keeps_only_live_rows(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    m = _export(conn, tmp_path / "d.jsonl", version="live", provenance={Provenance.LIVE})
    assert _rows(tmp_path / "d.jsonl") == []  # this database holds back-fill only
    assert m.content_hash.startswith("sha256:")


def test_a_version_cannot_be_reused_for_different_content(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    out = tmp_path / "d.jsonl"
    _export(conn, out)
    before = out.read_bytes()
    with pytest.raises(LifecycleRefused):
        _export(conn, out, holdout=6 * STEP)
    assert out.read_bytes() == before  # a refused export leaves the registered file


def test_a_label_not_yet_known_at_the_window_end_is_left_empty(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    end = _bar_close(36)  # holdout [bar_close(28), end); bar 35 is known 2 s after `end`
    _export(conn, tmp_path / "d.jsonl", version="w", window_end=end)
    rows = {r["as_of"]: r for r in _rows(tmp_path / "d.jsonl")}
    assert rows[_bar_close(31).isoformat()]["labels"]["M15x4"] is not None
    assert rows[_bar_close(32).isoformat()]["labels"]["M15x4"] is None  # known after the end


def test_only_the_requested_candle_dataset_versions_are_exported(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    SourceRegistry(conn).register("v2", "dukascopy")
    _record(conn, "v2")
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


def test_purge_uses_the_longest_horizon_and_rows_say_if_fully_labelled(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    two = Horizon(M15, 2)
    MarketRecorder(conn, candles=candles, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[two, H4], provenance=Provenance.BACKFILL,
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    _export(conn, tmp_path / "d.jsonl", version="two", horizons=[two, H4])
    rows = _rows(tmp_path / "d.jsonl")
    train0 = [r["as_of"] for r in rows if 0 in r["folds"]["train"]]
    assert _bar_close(9).isoformat() not in train0  # 2-bar label fits, 4-bar label does not
    assert all(r["labelled"] == all(v is not None for v in r["labels"].values()) for r in rows)
    assert any(not r["labelled"] for r in rows)  # the tail of the holdout
