"""Offline/online parity (#129): a model that reads the M04 transforms is fed, live, the
same values the training export writes for that bar, and its recorded predictions are
scored on the same triple-barrier label version the export carries. The raw `move` label
stays as it was."""

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
from app.learning.lifecycle import RATE_Q, DatasetManifest, DatasetStore, Split
from app.market_data.features import ATR_M15
from app.market_data.session import XAUUSD_NY_1700, SessionSchedule
from app.market_data.store import CandleStore
from app.prediction.card import ModelCard
from app.prediction.engine import load_baseline
from app.prediction.transforms import TRANSFORM_SCHEMA
from app.runtime.market_recorder import LOOKBACK, MarketRecorder

KEY = "M15.close_sma_atr"
N = LOOKBACK + 12


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


@pytest.fixture
def model() -> Stationary:
    return Stationary()


@pytest.fixture
def candles(conn: sqlite3.Connection, model: Stationary) -> CandleStore:
    store = CandleStore(conn, dataset_version="v1")
    _bars(store, [str(2000 + i) for i in range(N)])
    MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[H4], provenance=Provenance.BACKFILL,
                   predictors=[load_baseline(model, mode=Mode.DEMO)],
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    return store


END = _bar_close(N) + LAG


def _export_with_manifest(
    conn: sqlite3.Connection, out: Path, session: SessionSchedule | None = None
) -> tuple[list[dict], DatasetManifest]:  # type: ignore[type-arg]
    manifest = export_dataset(
        conn, symbol="XAUUSD", timeframe=M15, horizons=[H4], dataset_version="v1",
        window_start=_bar_close(0), window_end=END,
        splits=[Split(name="train", start=_bar_close(0), end=_bar_close(N - 20)),
                Split(name="validation", start=_bar_close(N - 20), end=_bar_close(N))],
        dataset_id="xau-tb", version="1", out=out, transforms=True, session=session,
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
            continue  # purged from the export: its window crosses the split end
        barrier = row["barrier_labels"][horizon_key(H4)]
        assert barrier["label_version"] == lab.label_version == LABEL_VERSION
        assert (barrier["outcome"], barrier["timeframe"]) == (lab.outcome, "M15")
        assert barrier["ends_at"] == lab.ends_at.isoformat()  # what a fold purge reads
        assert row["labels"][horizon_key(H4)]["move"] == "4"  # the raw move is unchanged
        compared += 1
    assert compared > 0
    # closes rise 1 a bar against an ATR of 4: the upper barrier is reached on the 4th bar
    assert {lab.outcome for _, lab in labelled} == {Outcome.UP}


def test_a_dual_touch_row_is_left_out_of_the_export_and_counted(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    store = CandleStore(conn, dataset_version="v1")
    spike = N - 30
    _bars(store, [str(2000 + i) for i in range(N)], skip={spike})
    t, p = T0 + spike * STEP, Decimal(2000 + spike)
    store.save(CandleClosed(symbol="XAUUSD", timeframe=M15, open_time=t, close_time=t + STEP,
                            open=p - 3, high=p + 10, low=p - 10, close=p, volume=Decimal(100),
                            spread=None, available_at=t + STEP + LAG))  # fmt: skip
    MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[H4], provenance=Provenance.BACKFILL,
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    exported, manifest = _export_with_manifest(conn, tmp_path / "d.jsonl")
    rows = {r["as_of"] for r in exported}
    ambiguous = _bar_close(spike - 1)  # its first barrier bar is the spike
    assert ambiguous.isoformat() not in rows and _bar_close(spike - 5).isoformat() in rows
    snaps = BarLog(conn).snapshots("XAUUSD", M15, start=T0, end=_bar_close(N - 5))
    stats = LabelStats.of([label_at(store, "XAUUSD", s.snapshot.as_of,
                                    s.snapshot.values.get(ATR_M15), H4, now=END)
                           for s in snaps])  # fmt: skip
    assert stats.ambiguous >= 1 and stats.dual_touch_rate is not None
    # the manifest carries the rate per era (each split) and for the whole export
    eras = {e.era: e for e in manifest.label_stats}
    assert set(eras) == {"all", "train", "validation"}
    total = eras["all"]
    assert total.dual_touch_count >= 1
    assert total.dual_touch_count == sum(eras[s].dual_touch_count for s in ("train", "validation"))
    known = total.rows - total.missing
    assert total.dual_touch_rate == (Decimal(total.dual_touch_count) / known).quantize(RATE_Q)
    assert total.flat_share is not None
    assert "session:24x7" in manifest.source_refs


def test_a_barrier_window_a_closure_stretches_past_the_split_end_is_purged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The raw purge (as_of + horizon <= split end) is not enough on a scheduled market:
    a closure inside the barrier window pushes its last slot past the split end."""
    store = CandleStore(conn, dataset_version="v1")
    closed = {88, 89, 90, 91}  # Monday 22:00-23:00 UTC: 17:00-18:00 New York (EST)
    _bars(store, [str(2000 + i) for i in range(N)], skip=closed)
    assert not any(XAUUSD_NY_1700.is_open(T0 + i * STEP) for i in closed)
    MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[H4], provenance=Provenance.BACKFILL, session=XAUUSD_NY_1700,
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    boundary = _bar_close(91)  # 23:00 UTC, the reopen
    export_dataset(conn, symbol="XAUUSD", timeframe=M15, horizons=[H4], dataset_version="v1",
                   window_start=_bar_close(0), window_end=END,
                   splits=[Split(name="train", start=_bar_close(0), end=boundary),
                           Split(name="validation", start=boundary, end=_bar_close(N))],
                   dataset_id="xau-tb-closure", version="1", out=tmp_path / "d.jsonl",
                   session=XAUUSD_NY_1700)  # fmt: skip
    manifest = DatasetStore(conn).get("xau-tb-closure", "1")
    assert manifest is not None and "session:xauusd-ny-1700" in manifest.source_refs
    rows = {
        json.loads(line)["as_of"]
        for line in (tmp_path / "d.jsonl").read_text(encoding="utf-8").splitlines()
    }
    assert _bar_close(83).isoformat() in rows  # its window ends at 22:00, before the closure
    for stretched in (_bar_close(86), _bar_close(87)):
        assert stretched + H4.length <= boundary  # the raw purge would keep it
        assert stretched.isoformat() not in rows


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
    """Train/serve session binding (#129): on XAUUSD both sides use XAUUSD_NY_1700. The
    first bar after the daily closure has a log return only on that schedule, so a
    recorder left on 24/7 would refuse it and this parity would break."""
    store = CandleStore(conn, dataset_version="v1")
    closed = {88, 89, 90, 91}  # Monday 22:00-23:00 UTC: 17:00-18:00 New York (EST)
    _bars(store, [str(2000 + i) for i in range(N)], skip=closed)
    model = SessionAware()
    MarketRecorder(conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[H4], provenance=Provenance.BACKFILL, session=XAUUSD_NY_1700,
                   predictors=[load_baseline(model, mode=Mode.DEMO)],
                   clock=Clock(_bar_close(N - 1) + LAG)).tick()  # fmt: skip
    exported, manifest = _export_with_manifest(conn, tmp_path / "d.jsonl", XAUUSD_NY_1700)
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
