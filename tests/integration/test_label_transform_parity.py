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
from tests.integration.test_market_recorder import H4, LAG, M15, T0, Clock, _bar_close, _bars

from app.core.enums import Direction, ForecastLineage, Mode
from app.learning.bars import BarLog, Provenance
from app.learning.export import export_dataset, horizon_key
from app.learning.labels import LABEL_VERSION, prediction_labels
from app.learning.lifecycle import Split
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


def _export(conn: sqlite3.Connection, out: Path) -> list[dict]:  # type: ignore[type-arg]
    export_dataset(conn, symbol="XAUUSD", timeframe=M15, horizons=[H4], dataset_version="v1",
                   window_start=_bar_close(0), window_end=END,
                   splits=[Split(name="train", start=_bar_close(0), end=_bar_close(N - 20)),
                           Split(name="validation", start=_bar_close(N - 20),
                                 end=_bar_close(N))],
                   dataset_id="xau-tb", version="1", out=out, transforms=True)  # fmt: skip
    return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]


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
                               now=END)  # fmt: skip
    labelled = [(p, lab) for p, lab in scored if lab is not None]
    assert labelled and scored[-1][1] is None  # the last bars' windows are not over
    for p, lab in labelled:
        row = rows.get(p.as_of.isoformat())
        if row is None:
            continue  # purged from the export: its window crosses the split end
        barrier = row["barrier_labels"][horizon_key(H4)]
        assert barrier["label_version"] == lab.label_version == LABEL_VERSION
        assert (barrier["long"], barrier["short"]) == (lab.long.outcome, lab.short.outcome)
        assert row["labels"][horizon_key(H4)]["move"] == "4"  # the raw move is unchanged
    # closes rise 1 a bar against an ATR of 4: the long reaches TP on the 4th bar
    assert {lab.long.outcome for _, lab in labelled} == {"UP"}
