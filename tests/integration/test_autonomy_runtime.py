"""The autonomous loop at runtime (#133): the champion and the shadowing candidates both
predict on live bars, only the champion's forecast reaches M05, a kill stops it at once,
and the scheduler entry point never raises.

Real adapter models (`barrier_softmax`) loaded through `load_model`, so hashes and
approvals are checked; the claim ledger is the fake from the unit tests."""

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from tests.support import tracer
from tests.unit.learning.test_autonomy import HASH, MODEL, T0, VALUES, World, _card

from app.core.bet import LABEL_VERSION
from app.core.contracts import FeatureSnapshot, ParameterBound, sha256_ref
from app.core.enums import DataQuality, HealthStatus, Mode
from app.learning.autonomy import Autonomy, CertifiedProtocol, Check, ModelFiles
from app.learning.bars import BarLog, Horizon, Provenance
from app.learning.lifecycle import OWNER_ACTOR, SYSTEM_ACTOR
from app.market_data.features import FEATURE_SCHEMA
from app.runtime.autonomy import AutonomyWorker, ChampionForecaster, ModelRoster, champion_only
from app.runtime.market_recorder import MarketRecorder
from app.runtime.news_worker import journal_evidence
from app.storage.artifacts import ArtifactLoader
from app.storage.journal import Journal, payload_as

D = Decimal
H1_DIR, M15_DIR = "H1.direction_10.direction", "M15.direction_10.direction"
M15 = tracer.M15


def _weights(lean: str) -> bytes:
    return json.dumps({"features": [H1_DIR, M15_DIR], "intercept": [lean, "0", "0"],
                       "coef": [["1.5", "1.0"], ["-1.5", "-1.0"], ["0", "0"]],
                       "temperature": "1"}).encode()  # fmt: skip


def _adapter_card(version: str, weights: bytes) -> Any:
    return _card(version, artifact_hash=tracer.ADAPTER_HASH, feature_schema=FEATURE_SCHEMA,
                 required_features=[H1_DIR, M15_DIR],
                 feature_ranges={k: ParameterBound(low=D(-1), high=D(1))
                                 for k in (H1_DIR, M15_DIR)},
                 weights_hash=sha256_ref(weights), label_version=LABEL_VERSION)  # fmt: skip


class Live(World):
    """The unit-test world plus loadable adapter models and recorded live snapshots."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(conn)
        self.mode = Mode.SHADOW
        self.loader = ArtifactLoader(self.registry, allowed_packages=("app.prediction.adapters",))
        self.roster = ModelRoster(self.life, self.artifacts, self.loader, model_id=MODEL)
        for version, lean in (("1", "0"), ("2", "0.5")):
            weights = _weights(lean)
            card = _adapter_card(version, weights)
            self.life.propose_candidate(card, dataset_refs=["gc@1"])
            self.artifacts.files_[card.ref] = ModelFiles(card, weights, tracer.ADAPTER)
        self.life.promote(MODEL, "1", modes=[Mode.SHADOW, Mode.DEMO], evidence_refs=["x02:r"],
                          policy_ref="p", approved_by=OWNER_ACTOR,
                          approval_ref="owner")  # fmt: skip
        self.ledger.protocols[HASH] = CertifiedProtocol(
            "agent", {**VALUES, "autonomy.feature_schema": FEATURE_SCHEMA}
        )
        self.ledger.parity[(HASH, f"{MODEL}@2")] = Check(True, "x02:run:parity-2")
        assert self.loop.submit("2", protocol_hash=HASH, mode=self.mode) == []

    def bar(self, as_of: datetime) -> None:
        BarLog(self.conn).record(FeatureSnapshot(
            symbol="XAUUSD", feature_schema=FEATURE_SCHEMA, values={H1_DIR: D(1), M15_DIR: D(1)},
            quality=DataQuality.OK, as_of=as_of, available_at=as_of + timedelta(seconds=1),
            valid_until=as_of + timedelta(minutes=15)), timeframe=M15, provenance=Provenance.LIVE,
            mode=self.mode, dataset_version="v1")  # fmt: skip

    def forecaster(self) -> ChampionForecaster:
        return ChampionForecaster(self.conn, self.roster, candles=self.candles, symbol="XAUUSD",
                                  timeframe=M15, account_mode=lambda: self.mode,
                                  clock=lambda: self.now)  # fmt: skip


@pytest.fixture
def live(conn: sqlite3.Connection) -> Live:
    return Live(conn)


def _journaled(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    from app.core.contracts import PredictionGenerated

    return [
        (p.model_id, p.model_version)
        for p in (
            payload_as(e, PredictionGenerated)
            for e in Journal(conn).recent(["prediction.generated"])
        )
    ]


def test_both_predict_on_live_bars_but_only_the_champion_reaches_m05(live: Live) -> None:
    """In account mode SHADOW the candidate is loadable for the account's own mode; the
    champion pointer alone decides what is journaled."""
    live.bar(T0)
    live.now = T0 + timedelta(seconds=5)
    shadow = live.roster.load("2", Mode.SHADOW, live.now)
    assert shadow is not None  # loadable in the account's own mode, SHADOW
    p = live.forecaster().tick()
    assert p is not None and (p.model_id, p.model_version) == (MODEL, "1")
    assert p.class_probabilities is not None
    assert live.forecaster().tick() is not None and _journaled(live.conn) == [(MODEL, "1")]
    ev = champion_only(journal_evidence(live.conn), live.life, model_id=MODEL,
                       account_mode=lambda: live.mode)(live.now)  # fmt: skip
    assert ev.forecast is not None and ev.forecast.model_version == "1"


def test_the_recorder_records_the_roster_with_provenance_and_never_journals_it(
    live: Live,
) -> None:
    live.now = T0 + timedelta(seconds=5)
    models = live.roster.recorded(live.loop, live.mode, live.now)
    assert [m.card.model_version for m in models] == ["1", "2"]
    assert models[1].mode is Mode.SHADOW
    log = BarLog(live.conn)
    live.bar(T0)
    snap = log.snapshots("XAUUSD", M15, start=T0, end=T0 + timedelta(minutes=1))[0].snapshot
    from app.prediction.engine import predict

    for m in models:  # what MarketRecorder._predict does for each roster model
        log.record_prediction(predict(m, snap, now=live.now), snap, provenance=Provenance.LIVE,
                              dataset_version="v1")  # fmt: skip
    rows = {r.prediction.model_version: r.provenance
            for r in log.predictions("XAUUSD", MODEL, start=T0, end=live.now)}  # fmt: skip
    assert rows == {"1": Provenance.LIVE, "2": Provenance.LIVE}
    assert _journaled(live.conn) == []


def test_recorder_use_predictors_swaps_the_recorded_models(live: Live) -> None:
    rec = MarketRecorder(live.conn, candles=live.candles, symbol="XAUUSD", timeframe=M15,
                         context_timeframes=[], horizons=[Horizon(M15, 4)])  # fmt: skip
    champion = live.roster.champion(live.mode, T0 + timedelta(seconds=5))
    assert champion is not None
    rec.use_predictors([champion])
    assert rec._predictors == [champion]
    from app.prediction.engine import Refused

    with pytest.raises(Refused, match="approved models only"):
        rec.use_predictors([champion.predictor])  # type: ignore[list-item]


def test_a_kill_stops_the_champion_forecast_at_once(live: Live) -> None:
    live.bar(T0)
    live.now = T0 + timedelta(seconds=5)
    assert live.forecaster().tick() is not None
    evidence = champion_only(journal_evidence(live.conn), live.life, model_id=MODEL,
                             account_mode=lambda: live.mode)  # fmt: skip
    assert evidence(live.now).forecast is not None
    live.life.withdraw(MODEL, approved_by=SYSTEM_ACTOR, reason="kill: test")
    live.now += timedelta(seconds=1)
    assert journal_evidence(live.conn)(live.now).forecast is not None  # still live in the journal
    assert evidence(live.now).forecast is None  # but not the champion's any more: M05 waits
    assert live.forecaster().tick() is None
    assert live.roster.champion(live.mode, live.now) is None


def test_a_revoked_approval_stops_a_cached_model(live: Live) -> None:
    now = T0 + timedelta(seconds=5)
    assert live.roster.champion(live.mode, now) is not None  # loaded and cached
    record = live.registry.eligibility("model", f"{MODEL}@1")
    assert record is not None
    live.registry.set_eligibility(record.model_copy(
        update={"expires_at": now + timedelta(seconds=1)}), actor=OWNER_ACTOR)  # fmt: skip
    assert live.roster.champion(live.mode, now + timedelta(seconds=2)) is None


# --- the scheduler entry point -----------------------------------------------------------------


def _health(conn: sqlite3.Connection) -> list[tuple[HealthStatus, str | None]]:
    from app.core.contracts import HealthUpdated

    out = []
    for e in reversed(Journal(conn).recent(["health.updated"])):
        h = payload_as(e, HealthUpdated)
        if h.component == "M07.autonomy":
            out.append((h.status, h.detail))
    return out


def test_the_worker_refuses_a_live_account_mode_and_never_raises(live: Live) -> None:
    modes = [Mode.LIVE]
    worker = AutonomyWorker(live.conn, live.loop, account_mode=lambda: modes[0],
                            clock=lambda: live.now)  # fmt: skip
    worker.tick()
    assert _health(live.conn)[-1][0] is HealthStatus.DEGRADED
    assert "SHADOW and DEMO only" in (_health(live.conn)[-1][1] or "")
    assert [r.action.value for _, r in live.loop.history()] == ["ADMIT"]  # nothing new
    modes[0] = Mode.DEMO
    live.now += timedelta(minutes=1)
    worker.tick()
    assert _health(live.conn)[-1][0] is HealthStatus.OK


def test_the_worker_backs_off_on_a_fault_and_refreshes_the_recorder(live: Live) -> None:
    class Broken:
        def certified(self, protocol_hash: str) -> None:
            raise RuntimeError("ledger down")

    broken = Autonomy(live.conn, lifecycle=live.life, ledger=Broken(),  # type: ignore[arg-type]
                      artifacts=live.artifacts, candles=live.candles, symbol="XAUUSD",
                      timeframe=M15, model_id=MODEL, protocol_hash=HASH,
                      clock=lambda: live.now)  # fmt: skip
    worker = AutonomyWorker(live.conn, broken, account_mode=lambda: Mode.DEMO,
                            clock=lambda: live.now)  # fmt: skip
    worker.tick()
    assert _health(live.conn)[-1][0] is HealthStatus.DOWN
    live.now += timedelta(minutes=1)
    worker.tick()  # inside the backoff: not retried
    assert len(_health(live.conn)) == 1
    rec = MarketRecorder(live.conn, candles=live.candles, symbol="XAUUSD", timeframe=M15,
                         context_timeframes=[], horizons=[Horizon(M15, 4)])  # fmt: skip
    live.now = T0 + timedelta(seconds=5)
    AutonomyWorker(live.conn, live.loop, roster=live.roster, recorder=rec,
                   account_mode=lambda: live.mode, clock=lambda: live.now).tick()  # fmt: skip
    assert [m.card.model_version for m in rec._predictors] == ["1", "2"]
