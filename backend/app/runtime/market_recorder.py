"""Records the bot's per-bar view for the training dataset (#108, PRD #107).

`MarketRecorder.tick()` catches up from the last recorded bar: for every bar of the
recording timeframe available by now, it builds the feature snapshot from exactly the
bars (of every configured timeframe) available at that bar's close, records it in the
M07 `BarLog`, then labels every recorded bar whose horizons have ended. The same code
back-fills historical candles: construct it with `Provenance.BACKFILL` and run `tick()`.

Given loaded M04 predictors, it also records what each one said about every newly
recorded bar, made at that bar's close (#109). A refused prediction (e.g. not enough
history for the model's features) is skipped. A model added later predicts only the
bars recorded after it was added. A model that fails is logged and skipped for that bar;
the snapshot and the other models are still recorded. Each prediction carries the bar's
provenance: a BACKFILL prediction may be in-sample for a model trained on those candles
(a card's training cutoff, since #127, refuses those bars). These are learning rows: never
journaled as `prediction.generated`, never trading evidence. Only approved
(`LoadedPredictor`) models are accepted: an evaluation-loaded model is refused at
construction, whatever the provenance.

A bar is tagged LIVE only when it is recorded within `LIVE_WITHIN` of becoming
available (the bot saw it as it happened); bars caught up after downtime, and every bar
of a back-fill, are BACKFILL and carry no account mode. Each pass reads only bars that
became available after the last recorded one and commits in one transaction. A bar that
arrives late (after a later bar was recorded) gets no row of its own: it leaks nothing,
but it is lost to the dataset.

Run it on its own connection, off the decision tick. It never raises: a failure is
logged, reported as DEGRADED health and retried after a backoff; the next good pass
reports OK. Nothing it writes is trading evidence. Wiring it into `serve` waits on the
MT5 adapter (#17).
"""

import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta

from app.core.contracts import FeatureSnapshot, HealthUpdated
from app.core.enums import HealthStatus, Mode, Timeframe
from app.core.events import make_event
from app.core.ids import stable_id
from app.core.time import TIMEFRAME_DURATION, Clock, utc_now
from app.learning.bars import BarLog, Horizon, Provenance
from app.market_data.features import (
    DEFAULT_TECHNIQUES,
    FEATURE_SCHEMA,
    build_feature_snapshot,
    lookback,
)
from app.market_data.session import SessionSchedule
from app.market_data.store import CandleStore
from app.market_data.techniques.indicators import Technique
from app.prediction.engine import LoadedPredictor, Refused, predict
from app.storage.config_store import ConfigStore
from app.storage.db import transaction
from app.storage.journal import Journal

__all__ = ["MarketRecorder"]

COMPONENT = "M07"
SOURCE = "m07"
RETRY_AFTER = timedelta(minutes=2)
"""A failed pass is retried after this, never on every tick [PROPOSED]."""
LIVE_WITHIN = 2
"""A bar recorded within this many bar lengths of becoming available is LIVE."""

log = logging.getLogger(__name__)


class MarketRecorder:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        candles: CandleStore,
        symbol: str,
        timeframe: Timeframe,
        context_timeframes: Sequence[Timeframe],
        horizons: Sequence[Horizon],
        provenance: Provenance = Provenance.LIVE,
        predictors: Sequence[LoadedPredictor] = (),
        clock: Clock = utc_now,
        account_id: str = "local-account",
        bot_id: str = "bot-1",
        session: SessionSchedule | None = None,
        techniques: Sequence[Technique] = DEFAULT_TECHNIQUES,
    ) -> None:
        if not all(isinstance(p, LoadedPredictor) for p in predictors):
            raise Refused("the recorder takes approved models only: never an evaluation load")
        self._conn, self._candles, self._symbol = conn, candles, symbol
        self._techniques = list(techniques)
        self._lookback = lookback(self._techniques)
        self._session = session  # the market's trading schedule; None means 24/7
        self._timeframes = [timeframe, *context_timeframes]
        self._horizons = list(horizons)
        step = TIMEFRAME_DURATION[timeframe]
        for h in self._horizons:
            unit = TIMEFRAME_DURATION[h.timeframe]
            if unit < step or unit % step:
                raise ValueError(f"horizon {h.timeframe.value} does not line up with "
                                 f"{timeframe.value} bars")  # fmt: skip
        self._provenance, self._clock = provenance, clock
        self._predictors = list(predictors)
        self._account_id, self._bot_id = account_id, bot_id
        self._log = BarLog(conn, clock=clock)
        self._retry_at: datetime | None = None
        self._healthy: bool | None = None

    def tick(self) -> None:
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return
        self._retry_at = None
        try:
            with transaction(self._conn):
                self._record(now)
                self._log.label_due(self._candles, symbol=self._symbol,
                                    horizons=self._horizons, now=now)  # fmt: skip
        except Exception as exc:  # report and back off; the recorder never propagates
            log.exception("M07 market recorder pass failed")
            self._retry_at = self._clock() + RETRY_AFTER
            self._report(HealthStatus.DEGRADED, f"market recorder: {type(exc).__name__}")
            return
        if self._healthy is not True:
            self._report(HealthStatus.OK, None)

    def _record(self, now: datetime) -> None:
        tf = self._timeframes[0]
        version = self._candles.dataset_version
        last = self._log.last_recorded(self._symbol, tf, FEATURE_SCHEMA, version)
        live_after = now - TIMEFRAME_DURATION[tf] * LIVE_WITHIN
        mode = self._mode()
        for bar in self._candles.bars(self._symbol, tf, as_of=now,
                                      after=None if last is None else last[1]):  # fmt: skip
            if last is not None and bar.close_time <= last[0]:
                continue  # a late bar: its slot is already past
            known = {
                t: self._candles.bars(self._symbol, t, as_of=bar.available_at,
                                      limit=self._lookback)
                for t in self._timeframes
            }  # fmt: skip
            snapshot = build_feature_snapshot(self._symbol, known, bar.available_at,
                                              self._techniques, session=self._session)  # fmt: skip
            live = self._provenance is Provenance.LIVE and bar.available_at >= live_after
            recorded = self._log.record(
                snapshot, timeframe=tf,
                provenance=Provenance.LIVE if live else Provenance.BACKFILL,
                mode=mode if live else None, dataset_version=version,
            )  # fmt: skip
            if recorded:
                self._predict(snapshot, Provenance.LIVE if live else Provenance.BACKFILL,
                              version)  # fmt: skip

    def _predict(self, snapshot: FeatureSnapshot, provenance: Provenance, version: str) -> None:
        """Each model in its own savepoint: one failing model never stops the snapshot or
        the other models from being recorded."""
        for loaded in self._predictors:
            try:
                with transaction(self._conn):
                    p = predict(loaded, snapshot, now=snapshot.available_at)
                    self._log.record_prediction(p, snapshot, provenance=provenance,
                                                dataset_version=version)  # fmt: skip
            except Refused:
                continue  # this model cannot speak to this bar: nothing to record
            except Exception:
                log.exception("M07 recorder: %s failed on %s", loaded.card.ref, snapshot.ref)

    def _mode(self) -> Mode | None:
        if self._provenance is not Provenance.LIVE:
            return None
        _, cfg = ConfigStore(self._conn).current()
        return Mode(cfg.trading.account_mode)

    def _report(self, status: HealthStatus, detail: str | None) -> None:
        self._healthy = status is HealthStatus.OK
        at = self._clock()
        try:
            Journal(self._conn).append(make_event(
                "health.updated", source=SOURCE,
                payload=HealthUpdated(component=COMPONENT, status=status,
                                      last_heartbeat=at, detail=detail),
                account_id=self._account_id, bot_id=self._bot_id,
                mode=self._mode() or Mode.SHADOW,
                event_id=stable_id("evt", COMPONENT, "recorder", at, status, detail),
                occurred_at=at,
            ))  # fmt: skip
        except Exception:
            log.exception("M07 market recorder could not report health")
