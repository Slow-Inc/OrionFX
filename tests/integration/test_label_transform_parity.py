"""Offline/online parity (#129): a model that reads the M04 transforms is fed, live, the
same values the training export writes for that bar, and its recorded predictions are
scored on the same triple-barrier label version the export carries. The raw `move` label
stays as it was.

Every version here is bound to `dukascopy` (#128), so the recorder, the export and label
scoring all read it on XAUUSD_NY_1700; the bars skip Monday's daily closure."""

import json
import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest
from tests.integration.test_market_recorder import (
    H4,
    LAG,
    M15,
    STEP,
    T0,
    Clock,
    _bar_close,
    _bars,
)

from app.core.contracts import CandleClosed
from app.core.enums import Direction, ForecastLineage, Mode
from app.learning.bars import BarLog, Provenance
from app.learning.export import export_dataset, horizon_key
from app.learning.labels import LABEL_VERSION, LabelStats, Outcome, label_at, prediction_labels
from app.learning.lifecycle import RATE_Q, DatasetManifest
from app.market_data.features import ATR_M15
from app.market_data.session import CME_GLOBEX_GC, XAUUSD_NY_1700, SessionSchedule
from app.market_data.sources import SourceRegistry
from app.market_data.store import CandleStore
from app.prediction.card import ModelCard
from app.prediction.engine import LoadedPredictor, load_baseline
from app.prediction.transforms import TRANSFORM_SCHEMA
from app.runtime.market_recorder import LOOKBACK, MarketRecorder

KEY = "M15.close_sma_atr"
N = LOOKBACK + 12
CLOSED = {88, 89, 90, 91}  # Monday 22:00-23:00 UTC: 17:00-18:00 New York (EST)
END = _bar_close(N) + LAG
PLAN = {"train": 31 * STEP, "test": 20 * STEP, "embargo": 4 * STEP, "holdout": 16 * STEP}
"""Four folds; fold 3 trains on [bar_close(60), bar_close(91)): it ends at the reopen."""


class Stationary:
    """A stand-in model on the transforms: it remembers what it was given."""

    card = ModelCard(
        model_id="tb-m15", model_version="0.1.0", artifact_hash=None,
        lineage=ForecastLineage.CHART_ONLY, feature_schema=TRANSFORM_SCHEMA,
        required_features=[KEY], feature_ranges={}, horizon=M15, horizon_bars=4,
        semantics="test rule; uncalibrated", calibrated=False, validation={},
        evidence_refs=[],
    )  # fmt: skip

    def __init__(self) -> None:
        self.seen: list[Decimal] = []

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        self.seen.append(features[KEY])
        return {Direction.UP: Decimal("0.6")}


def _gold(conn: sqlite3.Connection, version: str, *, base: int = 2000,
          skip: set[int] = frozenset()) -> CandleStore:  # type: ignore[assignment]  # fmt: skip
    """Rising M15 closes on the gold schedule, bound to `dukascopy`."""
    SourceRegistry(conn).register(version, "dukascopy")
    store = CandleStore(conn, dataset_version=version)
    _bars(store, [str(base + i) for i in range(N)], skip=CLOSED | skip)
    return store


def _record(conn: sqlite3.Connection, store: CandleStore, *predictors: LoadedPredictor,
            session: SessionSchedule | None = None) -> None:  # fmt: skip
    MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[H4], provenance=Provenance.BACKFILL, session=session,
                   predictors=list(predictors),
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip


@pytest.fixture
def model() -> Stationary:
    return Stationary()


@pytest.fixture
def candles(conn: sqlite3.Connection, model: Stationary) -> CandleStore:
    store = _gold(conn, "v1")
    _record(conn, store, load_baseline(model, mode=Mode.DEMO))
    return store


def _export_with_manifest(
    conn: sqlite3.Connection, out: Path, session: SessionSchedule | None = None,
    plan: Mapping[str, object] = PLAN,
) -> tuple[list[dict], DatasetManifest]:  # type: ignore[type-arg]  # fmt: skip
    manifest = export_dataset(
        conn, symbol="XAUUSD", timeframe=M15, horizons=[H4], dataset_versions=["v1"],
        window_start=_bar_close(0), window_end=_bar_close(N), dataset_id="xau-tb",
        version="1", out=out, transforms=True, session=session, **plan,
    )  # fmt: skip
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    return rows, manifest


def _export(conn: sqlite3.Connection, out: Path) -> list[dict]:  # type: ignore[type-arg]
    return _export_with_manifest(conn, out)[0]


def test_live_inputs_are_the_exported_transforms(
    conn: sqlite3.Connection, candles: CandleStore, model: Stationary, tmp_path: Path
) -> None:
    rows = {r["as_of"]: r for r in _export(conn, tmp_path / "d.jsonl")}
    assert {r["feature_schema"] for r in rows.values()} == {TRANSFORM_SCHEMA}
    assert all("tick_volume" not in k for r in rows.values() for k in r["features"])
    recorded = BarLog(conn).predictions("XAUUSD", "tb-m15", start=T0, end=END)
    assert len(recorded) == len(model.seen) > 0  # in bar order, one per predicted bar
    compared = 0
    for r, seen in zip(recorded, model.seen, strict=True):
        assert r.prediction.input_refs == [f"{TRANSFORM_SCHEMA}@{r.as_of.isoformat()}"]
        row = rows.get(r.as_of.isoformat())
        if row is not None:  # not purged
            assert Decimal(row["features"][KEY]) == seen
            compared += 1
    assert compared > 0
    flags = {r["barrier_labelled"] for r in rows.values()}
    assert flags == {True, False}  # early rows have no ATR, so no barrier label
    for r in rows.values():
        assert r["barrier_labelled"] == all(b is not None for b in r["barrier_labels"].values())


def test_predictions_are_scored_on_the_exported_label_version(
    conn: sqlite3.Connection, candles: CandleStore, tmp_path: Path
) -> None:
    rows = {r["as_of"]: r for r in _export(conn, tmp_path / "d.jsonl")}
    scored = prediction_labels(conn, candles, "XAUUSD", "tb-m15", M15, start=T0, end=END,
                               now=END, session=None)  # fmt: skip
    labelled = [(p, lab) for p, lab in scored if lab is not None]
    assert labelled and scored[-1][1] is None  # the last bars' windows are not over
    compared = 0
    for p, lab in labelled:
        row = rows.get(p.as_of.isoformat())
        if row is None:
            continue  # in no window: purged, an embargo gap, or past the holdout
        barrier = row["barrier_labels"][horizon_key(H4)]
        assert barrier["label_version"] == lab.label_version == LABEL_VERSION
        assert (barrier["outcome"], barrier["timeframe"]) == (lab.outcome, "M15")
        assert barrier["ends_at"] == lab.ends_at.isoformat()  # what the fold purge reads
        compared += 1
    assert compared > 0
    # closes rise 1 a bar against an ATR of 4: the upper barrier is reached first
    assert {lab.outcome for _, lab in labelled} == {Outcome.UP}


def test_a_dual_touch_row_is_left_out_of_the_export_and_counted(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    spike = 100  # after the closure, inside fold 3's test window
    store = _gold(conn, "v1", skip={spike})
    t, p = T0 + spike * STEP, Decimal(2000 + spike)
    store.save(CandleClosed(symbol="XAUUSD", timeframe=M15, open_time=t, close_time=t + STEP,
                            open=p - 3, high=p + 10, low=p - 10, close=p, volume=Decimal(100),
                            spread=None, available_at=t + STEP + LAG))  # fmt: skip
    _record(conn, store)
    exported, manifest = _export_with_manifest(conn, tmp_path / "d.jsonl")
    rows = {r["as_of"] for r in exported}
    ambiguous = _bar_close(spike - 1)  # its first barrier bar is the spike
    assert ambiguous.isoformat() not in rows and _bar_close(spike - 5).isoformat() in rows
    snaps = BarLog(conn).snapshots("XAUUSD", M15, start=T0, end=_bar_close(N - 5))
    stats = LabelStats.of([label_at(store, "XAUUSD", s.snapshot.as_of,
                                    s.snapshot.values.get(ATR_M15), H4, now=END,
                                    session=XAUUSD_NY_1700)
                           for s in snaps])  # fmt: skip
    assert stats.ambiguous >= 1 and stats.dual_touch_rate is not None
    # the manifest carries the rate per era (each split) and for the whole export
    eras = {e.era: e for e in manifest.label_stats}
    assert set(eras) == {"all", "development", "holdout"}
    total = eras["all"]
    assert total.dual_touch_count >= 1
    parts = eras["development"].dual_touch_count + eras["holdout"].dual_touch_count
    assert total.dual_touch_count == parts
    known = total.rows - total.missing
    assert total.dual_touch_rate == (Decimal(total.dual_touch_count) / known).quantize(RATE_Q)
    assert total.flat_share is not None
    assert "session:xauusd-ny-1700" in manifest.source_refs


def test_a_barrier_window_a_closure_stretches_past_a_fold_end_is_purged_from_it(
    candles: CandleStore, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The raw purge (as_of + horizon <= window end) is not enough on a scheduled market:
    a closure inside the barrier window pushes its last slot past the window's end."""
    exported, manifest = _export_with_manifest(conn, tmp_path / "d.jsonl")
    boundary = _bar_close(91)  # 23:00 UTC, the reopen
    assert manifest.folds[3].train_end == boundary
    rows = {r["as_of"]: r for r in exported}
    assert 3 in rows[_bar_close(83).isoformat()]["folds"]["train"]  # ends at 22:00
    for stretched in (_bar_close(86), _bar_close(87)):
        assert stretched + H4.length <= boundary  # the raw purge would keep it in fold 3
        row = rows.get(stretched.isoformat())
        assert row is None or 3 not in row["folds"]["train"]


class SessionAware(Stationary):
    """Reads the M15 log return, which spans a scheduled closure only on a schedule."""

    card = Stationary.card.model_copy(update={
        "model_id": "tb-m15-session", "required_features": [KEY, "M15.log_return"]})  # fmt: skip

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[dict[str, Decimal]] = []

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        self.inputs.append(dict(features))
        return super().predict(features)


def test_the_recorder_and_the_export_read_the_same_session_schedule(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Train/serve session binding (#129, #128): both read the version on its source's
    schedule. The first bar after the daily closure has a log return only on that
    schedule, so a recorder reading 24/7 would refuse it and break this parity; a
    schedule other than the source's is refused on either side."""
    store = _gold(conn, "v1")
    model = SessionAware()
    _record(conn, store, load_baseline(model, mode=Mode.DEMO), session=XAUUSD_NY_1700)
    # fold 2 tests on [bar_close(84), bar_close(104)): it holds the first bar after the closure
    exported, manifest = _export_with_manifest(conn, tmp_path / "d.jsonl", XAUUSD_NY_1700,
                                               {**PLAN, "train": 40 * STEP})  # fmt: skip
    assert "session:xauusd-ny-1700" in manifest.source_refs
    rows = {r["as_of"]: r for r in exported}
    recorded = BarLog(conn).predictions("XAUUSD", "tb-m15-session", start=T0, end=END)
    seen = {r.as_of: f for r, f in zip(recorded, model.inputs, strict=True)}
    reopen = _bar_close(92)  # the first bar after the closure
    assert reopen in seen and reopen.isoformat() in rows
    for as_of, features in seen.items():
        row = rows.get(as_of.isoformat())
        if row is not None:
            assert {k: Decimal(row["features"][k]) for k in features} == features
    with pytest.raises(ValueError, match="validated against"):
        _record(conn, store, session=CME_GLOBEX_GC)
    with pytest.raises(ValueError, match="validated against"):
        _export_with_manifest(conn, tmp_path / "e.jsonl", CME_GLOBEX_GC)


def test_predictions_are_scored_only_against_their_own_dataset_version(
    conn: sqlite3.Connection, candles: CandleStore, model: Stationary
) -> None:
    _record(conn, _gold(conn, "v2", base=3000), load_baseline(model, mode=Mode.DEMO))
    log = BarLog(conn)
    v1 = log.predictions("XAUUSD", "tb-m15", start=T0, end=END, dataset_version="v1")
    both = log.predictions("XAUUSD", "tb-m15", start=T0, end=END)
    assert len(both) == 2 * len(v1) > 0
    scored = prediction_labels(conn, candles, "XAUUSD", "tb-m15", M15, start=T0, end=END,
                               now=END, session=None)  # fmt: skip
    assert [p.prediction.prediction_id for p, _ in scored] == [
        p.prediction.prediction_id for p in v1]  # fmt: skip
