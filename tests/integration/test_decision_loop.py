"""X01 follow-up C (#84): the runtime decision loop against replayed bars and the real M06
engine on the fake broker. The loop decides and journals; only M06 trades."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tests.integration.conftest import T0
from tests.support.engine import Clock, _bot_positions, _running

from app.core.contracts import (
    CandleClosed,
    Confidence,
    DecisionCreated,
    EligibilityRecord,
    HealthUpdated,
    MarketSnapshot,
    PredictionGenerated,
    StrategyManifest,
    StrategyProposal,
    TradeIntent,
)
from app.core.enums import (
    CycleState,
    DataQuality,
    Direction,
    Eligibility,
    ForecastLineage,
    HealthStatus,
    IntentAction,
    Mode,
    Side,
    Timeframe,
)
from app.core.events import make_event
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


QUOTE_MAX_AGE = timedelta(seconds=10)


def _quote_of(broker: FakeBroker):  # type: ignore[no-untyped-def]
    """Test feed: the fake broker's last quote as M02 would report it."""

    def quote(now: datetime) -> MarketSnapshot:
        q = broker.quote("XAUUSD")
        if q is None:
            return MarketSnapshot(symbol="XAUUSD", bid=None, ask=None, spread=None,
                                  tick_time=None, quality=DataQuality.MISSING)  # fmt: skip
        fresh = now - q.time <= QUOTE_MAX_AGE
        return MarketSnapshot(symbol="XAUUSD", bid=q.bid, ask=q.ask, spread=q.ask - q.bid,
                              tick_time=q.time,
                              quality=DataQuality.OK if fresh else DataQuality.STALE)  # fmt: skip

    return quote


def _setup(conn: sqlite3.Connection, broker: FakeBroker, clock: Clock, *, evidence=_calibrated,  # type: ignore[no-untyped-def]
           context: list[Timeframe] | None = None, mode: str = "DEMO"):  # fmt: skip
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
    eng = _running(conn, broker, clock, strategies_enabled=["always_up"], account_mode=mode)
    loop = DecisionLoop(
        conn, candles=store, symbol="XAUUSD", timeframe=M15, context_timeframes=context or [],
        strategies=[AlwaysUp()], registry=reg, evidence=evidence, quote=_quote_of(broker),
        costs=Costs(contract_size=D(100), commission_per_lot=D(0)),
        experiences=ExperienceLog(conn, store, clock=clock), clock=clock,
    )  # fmt: skip
    return bars, loop, eng


def _at(broker: FakeBroker, eng: Engine, bar: CandleClosed) -> None:
    """Move to `bar` and let M06 run once, as it does continuously (fresh positions)."""
    broker.now = bar.available_at
    broker.set_quote("XAUUSD", bid=bar.close - D("0.10"), ask=bar.close + D("0.10"))
    eng.tick()


def _fresh_quote(broker: FakeBroker, bar: CandleClosed) -> None:
    """A new quote at the current time (time moved on within the same bar)."""
    broker.set_quote("XAUUSD", bid=bar.close - D("0.10"), ask=bar.close + D("0.10"))


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
    _fresh_quote(broker, bars[100])
    eng.tick()
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
    _fresh_quote(broker, bars[100])
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


def test_unchanged_health_is_not_rejournaled_every_tick_but_still_beats(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock, evidence=lambda now: Evidence())

    def beats() -> int:
        return sum(
            payload_as(e, HealthUpdated).component == "M05.loop"
            for e in Journal(conn).recent(["health.updated"], limit=100)
        )

    _at(broker, eng, bars[100])
    loop.tick()
    clock.advance(5)
    loop.tick()
    assert beats() == 1  # same status a moment later: nothing new
    clock.advance(60)
    _fresh_quote(broker, bars[100])
    eng.tick()  # inputs stay fresh: only the heartbeat can add a beat
    loop.tick()
    assert beats() == 2  # the heartbeat still says the loop is alive


def _last_health(conn: sqlite3.Connection) -> HealthUpdated:
    (beat,) = Journal(conn).recent(["health.updated"], limit=1)
    return payload_as(beat, HealthUpdated)


def _decisions(conn: sqlite3.Connection) -> int:
    return len(Journal(conn).recent(["decision.created"], limit=100))


def test_an_expired_undecided_intent_is_no_longer_pending(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    loop.tick()  # OPEN intent, never decided by M06 (the engine does not run on it)
    (intent,) = Journal(conn).recent(["trade.intent"], limit=1)
    expiry = payload_as(intent, TradeIntent).valid_until
    clock.advance((expiry - broker.now).total_seconds() + 1)
    broker.set_quote("XAUUSD", bid=bars[100].close, ask=bars[100].close + D("0.20"))
    loop._pending(broker.now)
    assert loop._pending(broker.now) == frozenset()


def test_a_stale_quote_means_no_decision(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    clock.advance(30)  # the quote is now 30 s old; positions are still fresh
    assert loop.tick() is None and _decisions(conn) == 0
    assert _last_health(conn).status is HealthStatus.DEGRADED
    assert "quote" in (_last_health(conn).detail or "")


def test_price_and_spread_come_from_the_live_quote_not_the_bar(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    broker.set_quote("XAUUSD", bid=D("1234.50"), ask=D("1234.80"))
    seen: list[tuple[Decimal | None, Decimal | None]] = []
    loop._strategies = [Spy(seen)]
    loop.tick()
    assert seen == [(D("1234.50"), D("0.30"))]


@dataclass(frozen=True)
class Spy:
    seen: list[tuple[Decimal | None, Decimal | None]]
    manifest: StrategyManifest = MANIFEST

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        self.seen.append((ctx.price, ctx.spread))
        return None


def test_a_missing_context_timeframe_is_not_silently_dropped(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock, context=[Timeframe.H1])  # no H1 bars stored
    _at(broker, eng, bars[100])
    d = loop.tick()
    assert d is not None and d.intent is None  # MISSING H1: no entry (it was ENTER before)
    assert d.created.action.value == "WAIT"


def test_no_open_cycle_but_bot_positions_reported_is_not_decided(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """A Bot Position with no open cycle known is a basket the loop cannot place: refuse."""
    from app.core.contracts import BotPosition, CycleUpdated, PositionsSnapshot
    from app.core.events import make_event

    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    (latest,) = Journal(conn).recent(["cycle.updated"], limit=1)
    closed = payload_as(latest, CycleUpdated).model_copy(update={"state": CycleState.CLOSED})
    Journal(conn).append(make_event(  # no open cycle: the loop must still look at positions
        "cycle.updated", source="m06", payload=closed, cycle_id=latest.envelope.cycle_id,
        account_id="local-account", bot_id="bot-1", mode=Mode.DEMO, occurred_at=broker.now,
    ))  # fmt: skip
    stray = BotPosition(position_id="p-x", symbol="XAUUSD", side=Side.BUY, volume=D("0.01"),
                        open_price=D("2000"), profit=D(0), commission=D(0), swap=D(0),
                        cycle_id="cycle-old")  # fmt: skip
    Journal(conn).append(make_event(
        "positions.snapshot", source="m06",
        payload=PositionsSnapshot(items=[stray], observed_at=broker.now),
        account_id="local-account", bot_id="bot-1", mode=Mode.DEMO,
    ))  # fmt: skip
    assert loop.tick() is None and _decisions(conn) == 0
    assert "outside the open cycle" in (_last_health(conn).detail or "")


def test_positions_not_reported_since_m06_last_decided_are_not_trusted(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """Between M06 allowing an OPEN and reporting the fill, the old snapshot says "no
    positions": the loop must not read that as room for a second entry."""
    from app.core.contracts import RiskDecision

    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    loop.tick()
    (intent,) = Journal(conn).recent(["trade.intent"], limit=1)
    clock.advance(1)
    from app.core.enums import Verdict
    from app.core.events import make_event

    allow = RiskDecision(intent_id=payload_as(intent, TradeIntent).intent_id,
                         verdict=Verdict.ALLOW, approved_lot=D("0.01"), policy_version="p",
                         reason_codes=[], decided_at=broker.now,
                         valid_until=broker.now + timedelta(seconds=30))  # fmt: skip
    Journal(conn).append(make_event(
        "risk.decision", source="m06", payload=allow, cycle_id=intent.envelope.cycle_id,
        account_id="local-account", bot_id="bot-1", mode=Mode.DEMO, occurred_at=broker.now,
    ))  # fmt: skip
    clock.advance(1)
    before = _decisions(conn)
    assert loop.tick() is None and _decisions(conn) == before
    assert "since M06's last decision" in (_last_health(conn).detail or "")


def test_a_closing_cycle_gets_no_new_decisions(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    from app.core.contracts import CycleUpdated
    from app.core.events import make_event

    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    (latest,) = Journal(conn).recent(["cycle.updated"], limit=1)
    closing = payload_as(latest, CycleUpdated).model_copy(update={"state": CycleState.CLOSING})
    Journal(conn).append(make_event(
        "cycle.updated", source="m06", payload=closing, cycle_id=latest.envelope.cycle_id,
        account_id="local-account", bot_id="bot-1", mode=Mode.DEMO, occurred_at=broker.now,
    ))  # fmt: skip
    assert loop.tick() is None and _decisions(conn) == 0


def test_the_cooldown_survives_the_next_cycle_opening(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    """M06 opens the next cycle right after closing one; the close time must still count."""
    from app.core.contracts import CycleUpdated
    from app.core.events import make_event

    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    (latest,) = Journal(conn).recent(["cycle.updated"], limit=1)
    upd = payload_as(latest, CycleUpdated)
    for state, cid in (
        (CycleState.CLOSED, latest.envelope.cycle_id),
        (CycleState.ACTIVE, "cyc-next"),
    ):
        Journal(conn).append(make_event(
            "cycle.updated", source="m06", payload=upd.model_copy(update={"state": state}),
            cycle_id=cid, account_id="local-account", bot_id="bot-1", mode=Mode.DEMO,
            occurred_at=broker.now,
        ))  # fmt: skip
    eng.tick()
    d = loop.tick()
    assert d is not None and "COOLDOWN" in d.created.reason_codes


def test_in_shadow_the_loop_decides_but_nothing_reaches_the_broker(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    bars, loop, eng = _setup(conn, broker, clock, mode="SHADOW")
    _at(broker, eng, bars[100])
    d = loop.tick()
    assert d is not None and d.intent is not None  # the decision is still made and recorded
    eng.tick()
    assert _bot_positions(broker) == []


def test_a_fault_while_reporting_a_fault_does_not_escape_the_tick(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    def broken(now: datetime) -> Evidence:
        raise RuntimeError("feed down")

    bars, loop, eng = _setup(conn, broker, clock, evidence=broken)
    _at(broker, eng, bars[100])

    def journal_down(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    loop._health = journal_down  # type: ignore[method-assign]
    assert loop.tick() is None  # reported to the log, not raised to the runner


def test_a_positions_snapshot_from_the_future_is_not_trusted(
    conn: sqlite3.Connection, broker: FakeBroker, clock: Clock
) -> None:
    from app.core.contracts import PositionsSnapshot

    bars, loop, eng = _setup(conn, broker, clock)
    _at(broker, eng, bars[100])
    Journal(conn).append(make_event(
        "positions.snapshot", source="m06",
        payload=PositionsSnapshot(items=[], observed_at=broker.now + timedelta(minutes=10)),
        account_id="local-account", bot_id="bot-1", mode=Mode.DEMO,
    ))  # fmt: skip
    assert loop.tick() is None and _decisions(conn) == 0
