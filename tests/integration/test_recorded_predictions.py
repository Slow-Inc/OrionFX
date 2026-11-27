"""M07 dataset slice B (#109, PRD #107): what each loaded prediction model said on each
recorded bar is kept as learning data, joined later to that bar's outcome label. It is
never journaled as `prediction.generated`, so recording it never changes what the bot
does."""

import sqlite3
from collections.abc import Mapping
from decimal import Decimal

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

from app.core.enums import Direction, ForecastLineage, Mode
from app.learning.bars import BarLog, Provenance
from app.market_data.features import FEATURE_SCHEMA
from app.market_data.store import CandleStore
from app.prediction.card import ModelCard
from app.prediction.engine import LoadedPredictor, load_baseline
from app.runtime.market_recorder import LOOKBACK, MarketRecorder
from app.runtime.news_worker import journal_evidence
from app.storage.journal import Journal

KEY = "M15.direction_10.direction"


class TrendM15:
    """A stand-in model: the M15 trend persists over 4 bars."""

    card = ModelCard(
        model_id="trend-m15", model_version="0.1.0", artifact_hash=None,
        lineage=ForecastLineage.CHART_ONLY, feature_schema=FEATURE_SCHEMA,
        required_features=[KEY], feature_ranges={}, horizon=M15, horizon_bars=4,
        semantics="test rule; uncalibrated", calibrated=False, validation={},
        evidence_refs=[],
    )  # fmt: skip

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        up = features[KEY] > 0
        return {Direction.UP if up else Direction.DOWN: Decimal("0.6")}


@pytest.fixture
def candles(conn: sqlite3.Connection) -> CandleStore:
    return CandleStore(conn, dataset_version="v1")


@pytest.fixture
def model() -> LoadedPredictor:
    return load_baseline(TrendM15(), mode=Mode.DEMO)


def _recorder(conn: sqlite3.Connection, candles: CandleStore, clock: Clock,
              model: LoadedPredictor) -> MarketRecorder:  # fmt: skip
    return MarketRecorder(conn, candles=candles, symbol="XAUUSD", timeframe=M15,
                          context_timeframes=[], horizons=[H4], provenance=Provenance.BACKFILL,
                          predictors=[model], clock=clock)  # fmt: skip


def test_each_bar_records_what_each_model_said_once(
    conn: sqlite3.Connection, candles: CandleStore, model: LoadedPredictor
) -> None:
    n = LOOKBACK + 12
    _bars(candles, _closes(n))
    clock = Clock(_bar_close(n - 1) + LAG)
    rec = _recorder(conn, candles, clock, model)
    rec.tick()
    rec.tick()  # idempotent
    rows = BarLog(conn).predictions("XAUUSD", "trend-m15", start=T0, end=clock.t)
    assert rows  # bars with enough history were predicted
    assert len({r.as_of for r in rows}) == len(rows)
    p = rows[-1].prediction
    assert p.direction is Direction.UP and p.model_version == "0.1.0"
    assert p.generated_at == rows[-1].as_of + LAG  # made at that bar's close, not later


def test_recorded_predictions_never_reach_trading(
    conn: sqlite3.Connection, candles: CandleStore, model: LoadedPredictor
) -> None:
    n = LOOKBACK + 6
    _bars(candles, _closes(n))
    now = _bar_close(n - 1) + LAG
    before = journal_evidence(conn)(now)
    _recorder(conn, candles, Clock(now), model).tick()
    assert BarLog(conn).predictions("XAUUSD", "trend-m15", start=T0, end=now)
    assert Journal(conn).recent(["prediction.generated"]) == []
    assert journal_evidence(conn)(now) == before  # same evidence, so the same decisions


def test_a_prediction_joins_the_outcome_of_its_bar_and_horizon(
    conn: sqlite3.Connection, candles: CandleStore, model: LoadedPredictor
) -> None:
    n = LOOKBACK + 12
    _bars(candles, _closes(n))
    clock = Clock(_bar_close(n - 1) + LAG)
    _recorder(conn, candles, clock, model).tick()
    pairs = BarLog(conn).prediction_outcomes("XAUUSD", "trend-m15", start=T0, end=clock.t)
    labelled = [(p, o) for p, o in pairs if o is not None]
    assert labelled and all(o.move == Decimal(4) for _, o in labelled)
    assert all(o.as_of == p.as_of for p, o in labelled)
    assert pairs[-1][1] is None  # the last bars' horizons have not ended


def test_a_refused_prediction_is_skipped_not_fatal(
    conn: sqlite3.Connection, candles: CandleStore, model: LoadedPredictor
) -> None:
    _bars(candles, _closes(3))  # far too little history for the feature the model needs
    clock = Clock(_bar_close(2) + LAG)
    _recorder(conn, candles, clock, model).tick()
    assert len(BarLog(conn).snapshots("XAUUSD", M15, start=T0, end=clock.t)) == 3
    assert BarLog(conn).predictions("XAUUSD", "trend-m15", start=T0, end=clock.t) == []
    assert Journal(conn).recent(["health.updated"])[0].envelope.payload.status.value == "OK"  # type: ignore[union-attr]


def test_predictions_are_append_only(
    conn: sqlite3.Connection, candles: CandleStore, model: LoadedPredictor
) -> None:
    n = LOOKBACK + 4
    _bars(candles, _closes(n))
    _recorder(conn, candles, Clock(_bar_close(n - 1) + LAG), model).tick()
    for sql in ("UPDATE learning_predictions SET body = '{}'", "DELETE FROM learning_predictions"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)


def test_the_outcome_matches_the_models_own_horizon_among_several(
    conn: sqlite3.Connection, candles: CandleStore, model: LoadedPredictor
) -> None:
    from app.learning.bars import Horizon

    n = LOOKBACK + 12
    _bars(candles, _closes(n))
    clock = Clock(_bar_close(n - 1) + LAG)
    MarketRecorder(conn, candles=candles, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
                   horizons=[Horizon(M15, 2), H4], provenance=Provenance.BACKFILL,
                   predictors=[model], clock=clock).tick()  # fmt: skip
    pairs = BarLog(conn).prediction_outcomes("XAUUSD", "trend-m15", start=T0, end=clock.t)
    outcomes = [o for _, o in pairs if o is not None]
    assert outcomes and {(o.horizon_bars, o.move) for o in outcomes} == {(4, Decimal(4))}
