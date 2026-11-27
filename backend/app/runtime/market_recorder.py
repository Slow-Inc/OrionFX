"""Records the bot's per-bar view for the training dataset (#108, PRD #107).

`MarketRecorder.tick()` catches up from the last recorded bar: for every bar of the
recording timeframe available by now, it builds the feature snapshot from exactly the
bars (of every configured timeframe) available at that bar's close, records it in the
M07 `BarLog`, then labels every recorded bar whose horizons have ended. The same code
back-fills historical candles: construct it with `Provenance.BACKFILL` and run `tick()`.

Run it on its own connection, off the decision tick. It never raises: a failure is
logged, reported as DEGRADED health and retried after a backoff. Nothing it writes is
trading evidence. Wiring it into `serve` waits on the MT5 adapter (#17).
"""

import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta

from app.core.contracts import HealthUpdated
from app.core.enums import HealthStatus, Mode, Timeframe
from app.core.events import make_event
from app.core.ids import stable_id
from app.core.time import Clock, utc_now
from app.learning.bars import BarLog, Horizon, Provenance
from app.market_data.features import DEFAULT_TECHNIQUES, FEATURE_SCHEMA, build_feature_snapshot
from app.market_data.store import CandleStore
from app.storage.config_store import ConfigStore
from app.storage.journal import Journal

__all__ = ["MarketRecorder"]

COMPONENT = "M07"
SOURCE = "m07"
LOOKBACK = max(t.lookback for t in DEFAULT_TECHNIQUES)
RETRY_AFTER = timedelta(minutes=2)
"""A failed pass is retried after this, never on every tick [PROPOSED]."""

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
        clock: Clock = utc_now,
        account_id: str = "local-account",
        bot_id: str = "bot-1",
    ) -> None:
        self._conn, self._candles, self._symbol = conn, candles, symbol
        self._timeframes = [timeframe, *context_timeframes]
        self._horizons = list(horizons)
        self._provenance, self._clock = provenance, clock
        self._account_id, self._bot_id = account_id, bot_id
        self._log = BarLog(conn, clock=clock)
        self._retry_at: datetime | None = None

    def tick(self) -> None:
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return
        self._retry_at = None
        try:
            self._record(now)
            self._log.label_due(self._candles, symbol=self._symbol, horizons=self._horizons,
                                now=now)  # fmt: skip
        except Exception as exc:  # report and back off; the recorder never propagates
            log.exception("M07 market recorder pass failed")
            self._retry_at = self._clock() + RETRY_AFTER
            self._report(f"market recorder: {type(exc).__name__}")

    def _record(self, now: datetime) -> None:
        tf = self._timeframes[0]
        version = self._candles.dataset_version
        last = self._log.last_as_of(self._symbol, tf, FEATURE_SCHEMA, version)
        mode = self._mode()
        for bar in self._candles.bars(self._symbol, tf, as_of=now):
            if last is not None and bar.close_time <= last:
                continue
            known = {
                t: self._candles.bars(self._symbol, t, as_of=bar.available_at, limit=LOOKBACK)
                for t in self._timeframes
            }
            snapshot = build_feature_snapshot(self._symbol, known, bar.available_at)
            self._log.record(snapshot, timeframe=tf, provenance=self._provenance, mode=mode,
                             dataset_version=version)  # fmt: skip

    def _mode(self) -> Mode | None:
        if self._provenance is not Provenance.LIVE:
            return None
        _, cfg = ConfigStore(self._conn).current()
        return Mode(cfg.trading.account_mode)

    def _report(self, detail: str) -> None:
        at = self._clock()
        try:
            Journal(self._conn).append(make_event(
                "health.updated", source=SOURCE,
                payload=HealthUpdated(component=COMPONENT, status=HealthStatus.DEGRADED,
                                      last_heartbeat=at, detail=detail),
                account_id=self._account_id, bot_id=self._bot_id,
                mode=self._mode() or Mode.SHADOW,
                event_id=stable_id("evt", COMPONENT, "recorder", at, detail), occurred_at=at,
            ))  # fmt: skip
        except Exception:
            log.exception("M07 market recorder could not report health")
