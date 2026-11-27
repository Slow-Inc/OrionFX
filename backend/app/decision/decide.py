"""Fusion, expected value after costs, WAIT rules, decisions and trade intents (#53;
DEC-01/02/04/05, UST-03; AT-14, AT-18, AT-19, AT-34).

`decide` is a pure function of its inputs: the same inputs give the same decision id,
so a repeat is recognised and refused. It emits a `decision.created` and, only for
ENTER, a `trade.intent`: a proposal M06 vetoes or allows, never a broker order.

WAIT is the default. In order:
- NO_OPPORTUNITY: no strategy proposed.
- CONFLICT: the proposing strategies disagree.
- POSITION_OPEN: the cycle already holds a position (adding is slice C).
- COOLDOWN: a cycle closed less than COOLDOWN ago.
- CONFLICT: the opposing evidence votes at least equal the agreeing ones (chart = 1).
- EV_UNKNOWN: there is no live calibrated probability for the direction, or a cost
  input or the ATR is missing.
- EV_NOT_POSITIVE: the expected value after spread and round-trip commission is <= 0.
- DUPLICATE: an identical decision is still pending.

Evidence votes once per independent source. A live forecast is one vote: the M04
ensemble has already merged its members. Live news assessments that cite a source
form one vote, by impact-weighted direction, unless the forecast is NEWS_ENHANCED
(it already contains the news). An LLM reasoning counts only when it
cites none of the news already counted. Stale or uncited evidence is ignored and is
not referenced.

EV = move x (2p - 1) - (spread + 2 x commission per lot x lot). Here move =
TARGET_ATR x M15 ATR x lot x contract size, and p is the live calibrated probability
of the chosen direction. Thresholds are [PROPOSED].
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.contracts import (
    DecisionCreated,
    LlmReasoning,
    NewsAssessment,
    PredictionGenerated,
    StrategyProposal,
    TradeIntent,
)
from app.core.enums import DecisionAction, Direction, ForecastLineage, IntentAction, Mode, Side
from app.core.events import EventEnvelope, make_event
from app.core.time import live
from app.decision.context import DecisionContext
from app.decision.strategies import ATR_M15

D = Decimal
CENT = D("0.01")
TARGET_ATR = D(1)
COOLDOWN = timedelta(minutes=15)
INTENT_TTL = timedelta(seconds=30)
SOURCE = "m05"


@dataclass(frozen=True)
class Costs:
    contract_size: Decimal
    commission_per_lot: Decimal
    """Charged per deal; a round trip pays it twice."""


@dataclass(frozen=True)
class CycleView:
    """Read-only view of the current cycle (M06 owns it)."""

    cycle_id: str | None
    open_positions: int
    last_closed_at: datetime | None
    pending_decisions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Evidence:
    forecast: PredictionGenerated | None = None
    news: list[NewsAssessment] = field(default_factory=list)
    reasoning: LlmReasoning | None = None


@dataclass(frozen=True)
class Decision:
    created: DecisionCreated
    intent: TradeIntent | None
    now: datetime


def _side(d: Direction) -> Side:
    return Side.BUY if d is Direction.UP else Side.SELL


def _signed(d: Direction, x: Decimal) -> Decimal:
    return x if d is Direction.UP else -x if d is Direction.DOWN else D(0)


def _votes(
    ctx: DecisionContext, ev: Evidence
) -> tuple[list[Direction], list[str], PredictionGenerated | None]:
    """Independent directional votes, the refs they rest on, and the live forecast."""
    now = ctx.now
    votes: list[Direction] = []
    refs: list[str] = []
    forecast = (
        ev.forecast
        if ev.forecast and live(ev.forecast.generated_at, ev.forecast.valid_until, now)
        else None
    )
    if forecast is not None and forecast.direction is not Direction.NEUTRAL:
        votes.append(forecast.direction)
        refs.append(forecast.prediction_id)
    news = [n for n in ev.news if n.source_refs and live(n.available_at, n.valid_until, now)]
    refs.extend(n.assessment_id for n in news)
    weight = sum((_signed(n.direction, n.impact) for n in news), D(0))
    news_in_forecast = forecast is not None and forecast.lineage is ForecastLineage.NEWS_ENHANCED
    if weight and not news_in_forecast:
        votes.append(Direction.UP if weight > 0 else Direction.DOWN)
    r = ev.reasoning
    if r is not None and now < r.valid_until:
        refs.append(r.reasoning_id)
        counted = {n.assessment_id for n in news}
        if r.direction is not Direction.NEUTRAL and not counted.intersection(r.news_assessment_ids):
            votes.append(r.direction)
    return votes, refs, forecast


def _ev(ctx: DecisionContext, p: Decimal | None, costs: Costs | None) -> Decimal | None:
    atr = ctx.value(ATR_M15)
    if p is None or costs is None or ctx.spread is None or atr is None:
        return None
    size = ctx.initial_lot * costs.contract_size
    move = TARGET_ATR * atr * size
    cost = ctx.spread * size + 2 * costs.commission_per_lot * ctx.initial_lot
    return (move * (2 * p - 1) - cost).quantize(CENT)


def _id(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:24]


def decide(
    ctx: DecisionContext,
    proposals: list[StrategyProposal],
    evidence: Evidence,
    *,
    cycle: CycleView,
    costs: Costs | None,
    symbol: str,
    policy_version: str,
) -> Decision:
    snap_ref = f"{ctx.snapshot.feature_schema}@{ctx.snapshot.as_of.isoformat()}"
    chosen = min(proposals, key=lambda p: p.strategy_id) if proposals else None
    strategy_refs = sorted(f"{p.strategy_id}@{p.strategy_version}" for p in proposals)
    votes, refs, forecast = _votes(ctx, evidence)
    evidence_refs = [snap_ref, *strategy_refs, *refs]
    basis = (snap_ref, strategy_refs, refs, cycle.cycle_id, ctx.mode, symbol, policy_version)

    direction = chosen.direction if chosen else None
    p = None
    if forecast is not None and forecast.direction is direction and forecast.probability.calibrated:
        p = forecast.probability.value
    ev = _ev(ctx, p, costs) if chosen else None
    entry_id = _id("dec_", "ENTER", *basis)

    reason: str | None = None
    if chosen is None:
        reason = "NO_OPPORTUNITY"
    elif len({q.direction for q in proposals}) > 1:
        reason = "CONFLICT"
    elif cycle.open_positions > 0:
        reason = "POSITION_OPEN"
    elif cycle.last_closed_at is not None and ctx.now - cycle.last_closed_at < COOLDOWN:
        reason = "COOLDOWN"
    elif sum(v is not direction for v in votes) >= 1 + sum(v is direction for v in votes):
        reason = "CONFLICT"
    elif ev is None:
        reason = "EV_UNKNOWN"
    elif ev <= 0:
        reason = "EV_NOT_POSITIVE"
    elif entry_id in cycle.pending_decisions:
        reason = "DUPLICATE"

    confidence = forecast.probability if p is not None and forecast else None
    if reason is not None:
        created = DecisionCreated(
            decision_id=_id("dec_", "WAIT", reason, *basis), action=DecisionAction.WAIT,
            strategy_id=chosen.strategy_id if chosen else None, side=None,
            expected_value_usd=ev, confidence=confidence, reason_codes=[reason],
            evidence_refs=evidence_refs, policy_version=policy_version,
        )  # fmt: skip
        return Decision(created, None, ctx.now)

    assert chosen is not None and direction is not None and confidence is not None
    created = DecisionCreated(
        decision_id=entry_id, action=DecisionAction.ENTER, strategy_id=chosen.strategy_id,
        side=_side(direction), expected_value_usd=ev, confidence=confidence,
        reason_codes=["EV_POSITIVE", f"VOTES_{sum(v is direction for v in votes) + 1}"],
        evidence_refs=evidence_refs, policy_version=policy_version,
    )  # fmt: skip
    intent = TradeIntent(
        intent_id=_id("int_", entry_id), decision_id=entry_id, action=IntentAction.OPEN,
        side=_side(direction), symbol=symbol, requested_lot=ctx.initial_lot,
        strategy_id=chosen.strategy_id, cycle_id=cycle.cycle_id, mode=ctx.mode,
        expected_value_usd=ev, prediction_ids=[forecast.prediction_id] if forecast else [],
        valid_until=min(ctx.now + INTENT_TTL, chosen.valid_until, ctx.snapshot.valid_until),
    )  # fmt: skip
    return Decision(created, intent, ctx.now)


def to_events(d: Decision, *, account_id: str, bot_id: str, mode: Mode) -> list[EventEnvelope]:
    """`decision.created`, then the `trade.intent` it caused. Ids are deterministic, so
    a republish is ignored by the journal."""

    def event(kind: str, payload: DecisionCreated | TradeIntent, key: str,
              causation: str | None) -> EventEnvelope:  # fmt: skip
        return make_event(
            kind, source=SOURCE, payload=payload, account_id=account_id, bot_id=bot_id,
            mode=mode, correlation_id=d.created.decision_id, causation_id=causation,
            cycle_id=d.intent.cycle_id if d.intent else None, event_id=_id("evt_", key),
            occurred_at=d.now,
        )  # fmt: skip

    first = event("decision.created", d.created, d.created.decision_id, None)
    if d.intent is None:
        return [first]
    return [first, event("trade.intent", d.intent, d.intent.intent_id, first.event_id)]
