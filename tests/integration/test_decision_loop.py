"""X01 follow-up C (#84): the runtime decision loop against replayed bars and the real M06
engine on the fake broker. The loop decides and journals; only M06 trades."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tests.integration.conftest import T0
from tests.support.engine import Clock, _bot_positions, _command, _completed, _configure, _engine

from app.core.contracts import (
    CandleClosed,
    Confidence,
    DecisionCreated,
    EligibilityRecord,
    HealthUpdated,
    PredictionGenerated,
    StrategyManifest,
    StrategyProposal,
    TradeIntent,
)
from app.core.enums import (
    ControlCommand,
    ControlState,
    DataQuality,
    Direction,
    Eligibility,
    ForecastLineage,
    HealthStatus,
    IntentAction,
    Mode,
    Timeframe,
)
from app.core.ids import new_id
from app.decision.context import DecisionContext
from app.decision.fusion import Costs, Evidence
from app.engine.engine import Engine
from app.execution.fake_broker import FakeBroker
from app.learning.experience import ExperienceLog
from app.market_data.store import CandleStore
from app.runtime.loop import DecisionLoop
from app.storage.journal import Journal, payload_as
from app.storage.registry import ArtifactRegistry
from app.validation.scenarios import Scenario, generate

D = Decimal
M15 = Timeframe.M15
MANIFEST = StrategyManifest(
    strategy_id="always_up", version="1", artifact_hash="sha256:" + "c" * 64, family="T",
    catalog_ids=["CAT-S01-01"], technique_refs=[], timeframes=[M15], warmup_bars=0,
    parameters={}, risk_tags=[], supported_modes=[Mode.SHADOW, Mode.DEMO],
)  # fmt: skip


@dataclass(frozen=True)
class AlwaysUp:
    """A test strategy: always proposes UP on M15. Not a real rule."""

    manifest: StrategyManifest = MANIFEST

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        return StrategyProposal(
            proposal_id=new_id("prop"), strategy_id="always_up", strategy_version="1",
            technique_refs=[], composition_id=None, decision_id=None, direction=Direction.UP,
            action=IntentAction.OPEN, horizon=M15, expected_value_usd=None,
            cost_estimate_usd=None, requested_lot=ctx.initial_lot,
            valid_until=ctx.snapshot.valid_until, rationale="test", constraints=[],
        )  # fmt: skip


def _calibrated(now: datetime) -> Evidence:
    return Evidence(forecast=PredictionGenerated(
        prediction_id=f"pred-{now.isoformat()}", model_id="m", model_version="1",
        lineage=ForecastLineage.CHART_ONLY, horizon=M15, horizon_bars=1, direction=Direction.UP,
        probability=Confidence(value=D("0.8"), semantics="calibrated P(up) over 1 bar",
                               calibrated=True),
        data_quality=DataQuality.OK, input_refs=[], generated_at=now,
        valid_until=now + timedelta(minutes=15),
    ))  # fmt: skip


def _setup(conn: sqlite3.Connection, broker: FakeBroker, clock: Clock, *, evidence=_calibrated):  # type: ignore[no-untyped-def]
    bars = generate(Scenario.STRONG_TREND, start=T0 - timedelta(days=1), bars=150)
    store = CandleStore(conn, dataset_version="v1")
    for b in bars:
        store.save(b)
    reg = ArtifactRegistry(conn, clock=lambda: T0 - timedelta(days=2))
    reg.register("strategy", MANIFEST, actor="m07")
    reg.set_eligibility(EligibilityRecord(
        subject_hash=MANIFEST.artifact_hash, subject_kind="strategy", subject_ref="always_up@1",
        user_enabled=True, eligibility=Eligibility.DEMO_ELIGIBLE,
        approved_modes=[Mode.SHADOW, Mode.DEMO], context={}, evidence_refs=["x02"],
        policy_version="p", expires_at=None, revoked_at=None,
    ), actor="owner")  # fmt: skip
    _configure(conn, strategies_enabled=["always_up"])
    eng = _engine(conn, broker, clock)
    assert eng.start()
    cid = _command(conn, ControlCommand.START)
    eng.tick()
    assert _completed(conn, cid).resulting_state is ControlState.RUNNING
    loop = DecisionLoop(
        conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=[],
        strategies=[AlwaysUp()], registry=reg, evidence=evidence,
        costs=Costs(contract_size=D(100), commission_per_lot=D(0)),
        experiences=ExperienceLog(conn, store, clock=clock), clock=clock,
    )  # fmt: skip
    return bars, loop, eng


def _at(broker: FakeBroker, eng: Engine, bar: CandleClosed) -> None:
    """Move to `bar` and let M06 run once, as it does continuously (fresh positions)."""
    broker.now = bar.available_at
    broker.set_quote("XAUUSD", bid=bar.close - D("0.10"), ask=bar.close + D("0.10"))
    eng.tick()


def test_a_tick_decides_journals_an_intent_and_m06_executes_it(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock)
    bar = bars[100]
    _at(broker, eng, bar)
    loop.tick()
    (dec,) = Journal(conn).recent(["decision.created"], limit=1)
    assert payload_as(dec, DecisionCreated).action.value == "ENTER"
    (intent,) = Journal(conn).recent(["trade.intent"], limit=1)
    assert payload_as(intent, TradeIntent).action is IntentAction.OPEN
    assert _bot_positions(broker) == []  # the loop never trades: only M06 does
    eng.tick()
    assert len(_bot_positions(broker)) == 1


def test_every_tick_is_recorded_as_experience_and_a_repeat_is_idempotent(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock, evidence=lambda now: Evidence())
    _at(broker, eng, bars[100])
    loop.tick()
    loop.tick()  # same bar, same inputs: same decision id, nothing new
    decisions = Journal(conn).recent(["decision.created"], limit=10)
    assert len(decisions) == 1
    assert payload_as(decisions[0], DecisionCreated).reason_codes == ["EV_UNKNOWN"]
    assert len(loop.experiences.experiences()) == 1


def test_with_a_position_open_the_loop_manages_instead_of_entering_again(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    loop.tick()
    eng.tick()
    _at(broker, eng, bars[101])
    loop.tick()
    (dec,) = Journal(conn).recent(["decision.created"], limit=1)
    # manage adds to a basket the evidence still supports; decide would only say POSITION_OPEN
    assert payload_as(dec, DecisionCreated).action.value == "ADD"


def test_a_failing_tick_reports_health_and_backs_off(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    calls = 0

    def broken(now: datetime) -> Evidence:
        nonlocal calls
        calls += 1
        raise RuntimeError("feed down")

    bars, loop, eng = _setup(conn, broker, clock, evidence=broken)
    _at(broker, eng, bars[100])
    loop.tick()
    (beat,) = Journal(conn).recent(["health.updated"], limit=1)
    h = payload_as(beat, HealthUpdated)
    assert h.component == "M05.loop" and h.status is HealthStatus.DOWN
    loop.tick()  # inside the backoff: not retried yet
    assert calls == 1
    clock.advance(120)
    loop.tick()
    assert calls == 2


def test_an_open_cycle_with_stale_positions_is_not_decided(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """Fail closed: with a cycle open, the loop must know the bot's positions to choose
    between entering and managing; a stale or missing snapshot means it does not know."""
    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    loop.tick()
    eng.tick()
    before = len(Journal(conn).recent(["decision.created"], limit=50))
    clock.advance(300)  # the engine has not reported positions for 5 minutes
    assert loop.tick() is None
    assert len(Journal(conn).recent(["decision.created"], limit=50)) == before
    (beat,) = Journal(conn).recent(["health.updated"], limit=1)
    h = payload_as(beat, HealthUpdated)
    assert h.status is HealthStatus.DEGRADED and "positions" in (h.detail or "")


def test_an_open_intent_m06_has_not_decided_blocks_a_second_entry(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    loop.tick()  # OPEN intent journaled; M06 has not run since
    clock.advance(10)
    loop.tick()
    (dec,) = Journal(conn).recent(["decision.created"], limit=1)
    assert payload_as(dec, DecisionCreated).reason_codes == ["DUPLICATE"]


def test_outcomes_are_labelled_once_the_horizon_has_passed(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock, evidence=lambda now: Evidence())
    _at(broker, eng, bars[100])
    loop.tick()
    assert loop.experiences.labels(as_of=bars[100].available_at) == {}
    _at(broker, eng, bars[103])
    loop.tick()
    assert len(loop.experiences.labels(as_of=bars[103].available_at)) >= 1


def test_an_intent_m06_already_decided_is_no_longer_pending(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    loop.tick()
    clock.advance(10)
    eng.tick()  # M06 executes the OPEN and reports the position
    clock.advance(10)
    loop.tick()  # the OPEN is decided, not pending: no DUPLICATE
    (dec,) = Journal(conn).recent(["decision.created"], limit=1)
    assert "DUPLICATE" not in payload_as(dec, DecisionCreated).reason_codes
