"""Runtime wiring of the autonomous loop (#133; ADR 0001; `app.learning.autonomy`).

No new process manager: like `MarketRecorder` and `NewsWorker`, each piece is a `tick()`
the runtime calls on its own cadence, on its own connection.

- `AutonomyWorker.tick()` is the scheduler entry point. It reads the account mode (an
  injected reader; the config store by default), runs one `Autonomy.step`, then refreshes
  the recorder's models (`MarketRecorder.use_predictors`) to the roster. It never
  raises: a refusal (no certified policy, a mode outside SHADOW/DEMO) reports DEGRADED and
  does nothing; a fault reports DOWN and backs off.
- `ModelRoster` loads the champion (for the account mode) and every shadowing candidate
  (for SHADOW) through `load_model`, so hashes and approvals are checked. A cached model
  is re-checked against the registry at every use, so a revoked or expired approval stops
  it at once.
- `ChampionForecaster.tick()` journals the champion's `prediction.generated` for the
  newest recorded LIVE snapshot. Only the champion pointer selects it: a shadowing
  candidate is approved for SHADOW, but it is never journaled, so it never feeds M05.
- `champion_only(base, ...)` wraps the decision loop's evidence (`journal_evidence`): a
  forecast that is not from the current, approved champion is dropped (with any reasoning
  that cites it), so a killed model's last forecast never outlives its approval.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from app.core.contracts import HealthUpdated, PredictionGenerated
from app.core.enums import HealthStatus, Mode, Timeframe
from app.core.events import make_event
from app.core.ids import stable_id
from app.core.time import TIMEFRAME_DURATION, Clock, utc_now
from app.decision.fusion import Evidence
from app.learning.autonomy import Autonomy, AutonomyRefused, ModelArtifacts
from app.learning.bars import BarLog, Provenance
from app.learning.lifecycle import ModelLifecycle
from app.market_data.features import FEATURE_SCHEMA
from app.market_data.session import SessionSchedule
from app.market_data.store import CandleStore
from app.prediction.engine import LoadedPredictor, Refused, load_model, predict
from app.prediction.transforms import TRANSFORM_SCHEMA, transform_from_store
from app.runtime.market_recorder import MarketRecorder
from app.storage.artifacts import ArtifactLoader
from app.storage.config_store import ConfigStore
from app.storage.journal import Journal
from app.storage.registry import subject_ref

__all__ = ["AutonomyWorker", "ChampionForecaster", "ModelRoster", "champion_only"]

COMPONENT = "M07.autonomy"
SOURCE = "m07"
FORECAST_SOURCE = "m04"
BACKOFF = timedelta(minutes=5)
"""A failed pass is retried after this, never on every tick [PROPOSED]."""

ModeReader = Callable[[], Mode]
log = logging.getLogger(__name__)


def config_mode(conn: sqlite3.Connection) -> ModeReader:
    return lambda: Mode(ConfigStore(conn).current()[1].trading.account_mode)


class ModelRoster:
    """Loaded models by approval: the champion and the shadowing candidates."""

    def __init__(self, lifecycle: ModelLifecycle, artifacts: ModelArtifacts,
                 loader: ArtifactLoader, *, model_id: str) -> None:  # fmt: skip
        self._life, self._artifacts, self._loader = lifecycle, artifacts, loader
        self._model_id = model_id
        self._cache: dict[tuple[str, Mode], LoadedPredictor] = {}

    def load(self, version: str, mode: Mode, now: datetime) -> LoadedPredictor | None:
        """The model approved for `mode` at `now`, or None."""
        ref = subject_ref(self._model_id, version)
        record = self._life.registry.eligibility_at("model", ref, now)
        if record is None or not record.approves(mode, now):
            self._cache.pop((version, mode), None)
            return None
        cached = self._cache.get((version, mode))
        if cached is not None and (cached.approved_until or now) == (record.expires_at or now):
            return cached
        files = self._artifacts.files(self._model_id, version)
        if files is None:
            return None
        try:
            loaded = load_model(self._loader, self._life.registry, model_id=self._model_id,
                                version=version, module=files.module, mode=mode, now=now,
                                card=files.card, weights=files.weights)  # fmt: skip
        except Refused:
            log.exception("M07 autonomy: %s refused to load", ref)
            return None
        self._cache[(version, mode)] = loaded
        return loaded

    def champion(self, mode: Mode, now: datetime) -> LoadedPredictor | None:
        version = self._life.champion(self._model_id)
        return None if version is None else self.load(version, mode, now)

    def recorded(self, autonomy: Autonomy, mode: Mode, now: datetime) -> list[LoadedPredictor]:
        """What the recorder records: the champion and every shadowing candidate."""
        models = [
            self.champion(mode, now),
            *(self.load(s.version, Mode.SHADOW, now) for s in autonomy.shadowing()),
        ]
        return [m for m in models if m is not None]  # fmt: skip


class AutonomyWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        autonomy: Autonomy,
        *,
        roster: ModelRoster | None = None,
        recorder: MarketRecorder | None = None,
        account_mode: ModeReader | None = None,
        clock: Clock = utc_now,
        account_id: str = "local-account",
        bot_id: str = "bot-1",
    ) -> None:
        self._conn, self._autonomy = conn, autonomy
        self._roster, self._recorder = roster, recorder
        self._mode = account_mode or config_mode(conn)
        self._clock, self._account_id, self._bot_id = clock, account_id, bot_id
        self._retry_at: datetime | None = None

    def tick(self) -> None:
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return
        self._retry_at = None
        mode = Mode.SHADOW
        try:
            mode = self._mode()
            done = self._autonomy.step(mode=mode)
            if self._roster is not None and self._recorder is not None:
                self._recorder.use_predictors(self._roster.recorded(self._autonomy, mode, now))
        except AutonomyRefused as refused:
            self._report(HealthStatus.DEGRADED, str(refused), mode, now)
            return
        except Exception as exc:  # report and back off; the loop never propagates
            log.exception("M07 autonomy pass failed")
            self._retry_at = now + BACKOFF
            self._report(HealthStatus.DOWN, f"autonomy: {type(exc).__name__}", mode, now)
            return
        detail = ", ".join(f"{r.action.value} {r.version}" for r in done) or None
        self._report(HealthStatus.OK, detail, mode, now)

    def _report(self, status: HealthStatus, detail: str | None, mode: Mode, at: datetime) -> None:
        try:
            Journal(self._conn).append(make_event(
                "health.updated", source=SOURCE,
                payload=HealthUpdated(component=COMPONENT, status=status, last_heartbeat=at,
                                      detail=detail),
                account_id=self._account_id, bot_id=self._bot_id,
                mode=mode,
                event_id=stable_id("evt", COMPONENT, at, status, detail), occurred_at=at,
            ))  # fmt: skip
        except Exception:
            log.exception("M07 autonomy could not report health")


class ChampionForecaster:
    """Journals the champion's forecast on the newest recorded LIVE bar, made at `now`."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        roster: ModelRoster,
        *,
        candles: CandleStore,
        symbol: str,
        timeframe: Timeframe,
        session: SessionSchedule | None = None,
        account_mode: ModeReader | None = None,
        clock: Clock = utc_now,
        account_id: str = "local-account",
        bot_id: str = "bot-1",
    ) -> None:
        self._conn, self._roster, self._candles = conn, roster, candles
        self._symbol, self._timeframe, self._session = symbol, timeframe, session
        self._mode = account_mode or config_mode(conn)
        self._clock, self._account_id, self._bot_id = clock, account_id, bot_id

    def tick(self) -> PredictionGenerated | None:
        now = self._clock()
        mode = self._mode()
        loaded = self._roster.champion(mode, now)
        if loaded is None:
            return None
        rows = [r for r in BarLog(self._conn).snapshots(
            self._symbol, self._timeframe, start=now - 2 * TIMEFRAME_DURATION[self._timeframe],
            end=now) if r.provenance is Provenance.LIVE
            and r.dataset_version == self._candles.dataset_version
            and r.snapshot.feature_schema == FEATURE_SCHEMA]  # fmt: skip
        if not rows:
            return None
        snapshot = rows[-1].snapshot
        if loaded.card.feature_schema == TRANSFORM_SCHEMA:
            snapshot = transform_from_store(self._candles, snapshot, session=self._session)
        try:
            p = predict(loaded, snapshot, now=now)
        except Refused:
            return None  # a refused prediction is no forecast: M05 waits
        Journal(self._conn).append(make_event(
            "prediction.generated", source=FORECAST_SOURCE, payload=p,
            account_id=self._account_id, bot_id=self._bot_id, mode=mode,
            event_id=stable_id("evt", "M04", loaded.card.ref, snapshot.as_of), occurred_at=now,
        ))  # fmt: skip
        return p


def champion_only(
    base: Callable[[datetime], Evidence],
    lifecycle: ModelLifecycle,
    *,
    model_id: str,
    account_mode: ModeReader,
) -> Callable[[datetime], Evidence]:
    """The decision loop's evidence with only the current champion's forecast."""

    def evidence(now: datetime) -> Evidence:
        ev = base(now)
        f = ev.forecast
        if f is None:
            return ev
        version = lifecycle.champion(model_id)
        record = None if version is None else lifecycle.registry.eligibility_at(
            "model", subject_ref(model_id, version), now)  # fmt: skip
        if (f.model_id, f.model_version) == (model_id, version) and record is not None \
                and record.approves(account_mode(), now):  # fmt: skip
            return ev
        cites = ev.reasoning is not None and f.prediction_id in ev.reasoning.prediction_ids
        return replace(ev, forecast=None, reasoning=None if cites else ev.reasoning)

    return evidence
