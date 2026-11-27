"""Fusion, expected value after costs, WAIT rules, decisions and trade intents (#53;
DEC-01/02/04/05, UST-03; AT-14, AT-18, AT-19, AT-34).

`decide` is a pure function of its inputs: the same inputs give the same decision id,
so a repeat is recognised and refused. It emits a `decision.created` and, only for
ENTER, a `trade.intent`: a proposal M06 vetoes or allows, never a broker order.

WAIT is the default (`WaitReason`, checked in this order):
- STALE_INPUT: the feature snapshot is not live at `now`.
- INVALID_LOT: the owner's initial lot is not positive.
- NO_OPPORTUNITY: no live OPEN proposal with a direction.
- CONFLICT_STRATEGIES: the proposing strategies disagree.
- POSITION_OPEN: the cycle already holds a position (adding is slice C).
- COOLDOWN: a cycle closed less than COOLDOWN ago.
- CONFLICT_EVIDENCE: opposing votes >= agreeing votes (the chart counts as one).
- EV_UNKNOWN: no usable probability for the trade, or a cost input / the ATR missing.
- EV_NOT_POSITIVE: EV after spread and round-trip commission is <= 0.
- DUPLICATE: an identical decision is still pending.

Evidence votes once per independent source:
- the forecast (one vote: the M04 ensemble already merged its members), when live and
  of usable data quality;
- live news assessments that cite a source, as one impact-weighted vote, unless the
  forecast is NEWS_ENHANCED (it already contains them);
- an LLM reasoning, only if it restates nothing already voting (no voting or
  forecast-covered news, not the forecast) and every news item it cites is live and
  cited; otherwise it is ignored and not referenced.

EV = move x (2p - 1) - (spread x size + 2 x commission per lot x lot), size = lot x
contract size, move = TARGET_ATR x M15 ATR x size. p is the forecast's calibrated
probability, used only when the forecast agrees, has OK data and the trade's horizon.
EV is computed only once every other gate passed. Thresholds are [PROPOSED].
"""

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
from app.core.enums import (
    DataQuality,
    DecisionAction,
    Direction,
    ForecastLineage,
    IntentAction,
    Mode,
    Side,
    WaitReason,
)
from app.core.events import EventEnvelope, make_event
from app.core.ids import stable_id
from app.core.time import live
from app.decision.context import DecisionContext
from app.decision.strategies import ATR_M15
from app.execution.broker import SymbolSpec
from app.market_data.features import USABLE_QUALITY

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

    @classmethod
    def from_spec(cls, spec: SymbolSpec, commission_per_lot: Decimal) -> "Costs":
        return cls(spec.contract_size, commission_per_lot)


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
    mode: Mode
    cycle_id: str | None


@dataclass(frozen=True)
class _Votes:
    votes: list[Direction]
    refs: list[str]


def _signed(d: Direction, x: Decimal) -> Decimal:
    return x if d is Direction.UP else -x if d is Direction.DOWN else D(0)


def _live_forecast(ev: Evidence, now: datetime) -> PredictionGenerated | None:
    f = ev.forecast
    if f is None or f.data_quality not in USABLE_QUALITY:
        return None
    return f if live(f.generated_at, f.valid_until, now) else None


def _votes(ev: Evidence, forecast: PredictionGenerated | None, now: datetime) -> _Votes:
    votes: list[Direction] = []
    refs: list[str] = []
    if forecast is not None and forecast.direction is not Direction.NEUTRAL:
        votes.append(forecast.direction)
        refs.append(forecast.prediction_id)
    news = [n for n in ev.news if n.source_refs and live(n.available_at, n.valid_until, now)]
    news_ids = {n.assessment_id for n in news}
    refs.extend(n.assessment_id for n in news)
    weight = sum((_signed(n.direction, n.impact) for n in news), D(0))
    in_forecast = forecast is not None and forecast.lineage is ForecastLineage.NEWS_ENHANCED
    if weight and not in_forecast:
        votes.append(Direction.UP if weight > 0 else Direction.DOWN)
    already_voting = news_ids if (weight or in_forecast) else set()

    r = ev.reasoning
    if r is not None and now < r.valid_until and set(r.news_assessment_ids) <= news_ids:
        refs.append(r.reasoning_id)
        restates = already_voting.intersection(r.news_assessment_ids) or (
            forecast is not None and forecast.prediction_id in r.prediction_ids
        )
        if r.direction is not Direction.NEUTRAL and not restates:
            votes.append(r.direction)
    return _Votes(votes, refs)


def _probability(forecast: PredictionGenerated | None, chosen: StrategyProposal) -> Decimal | None:
    if (
        forecast is None
        or forecast.direction is not chosen.direction
        or not forecast.probability.calibrated
        or forecast.data_quality is not DataQuality.OK
        or forecast.horizon is not chosen.horizon
    ):
        return None
    return forecast.probability.value


def _ev(ctx: DecisionContext, p: Decimal | None, costs: Costs | None) -> Decimal | None:
    atr = ctx.value(ATR_M15)
    if p is None or costs is None or ctx.spread is None or atr is None:
        return None
    size = ctx.initial_lot * costs.contract_size
    cost = ctx.spread * size + 2 * costs.commission_per_lot * ctx.initial_lot
    return (TARGET_ATR * atr * size * (2 * p - 1) - cost).quantize(CENT)


def _side(d: Direction) -> Side:
    if d is Direction.NEUTRAL:
        raise ValueError("a NEUTRAL direction has no side")
    return Side.BUY if d is Direction.UP else Side.SELL


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
    now = ctx.now
    usable = sorted(
        (p for p in proposals if p.action is IntentAction.OPEN
         and p.direction is not Direction.NEUTRAL and now < p.valid_until),
        key=lambda p: p.strategy_id,
    )  # fmt: skip
    chosen = usable[0] if usable else None
    forecast = _live_forecast(evidence, now)
    v = _votes(evidence, forecast, now)
    strategy_refs = [f"{p.strategy_id}@{p.strategy_version}" for p in usable]
    evidence_refs = [ctx.snapshot.ref, *strategy_refs, *v.refs]
    cost_basis = None if costs is None else [costs.contract_size, costs.commission_per_lot]
    basis = [evidence_refs, cycle.cycle_id, ctx.mode, symbol, policy_version,
             ctx.initial_lot, ctx.spread, cost_basis]  # fmt: skip
    entry_id = stable_id("dec", "ENTER", basis)

    direction = chosen.direction if chosen else None
    agree = 1 + v.votes.count(direction) if direction else 0
    against = len(v.votes) - v.votes.count(direction) if direction else 0
    ev: Decimal | None = None
    confidence = None

    reason: WaitReason | None = None
    if not live(ctx.snapshot.available_at, ctx.snapshot.valid_until, now):
        reason = WaitReason.STALE_INPUT
    elif ctx.initial_lot <= 0:
        reason = WaitReason.INVALID_LOT
    elif chosen is None:
        reason = WaitReason.NO_OPPORTUNITY
    elif len({p.direction for p in usable}) > 1:
        reason = WaitReason.CONFLICT_STRATEGIES
    elif cycle.open_positions > 0:
        reason = WaitReason.POSITION_OPEN
    elif cycle.last_closed_at is not None and now - cycle.last_closed_at < COOLDOWN:
        reason = WaitReason.COOLDOWN
    elif against >= agree:
        reason = WaitReason.CONFLICT_EVIDENCE
    else:
        p = _probability(forecast, chosen)
        ev = _ev(ctx, p, costs)
        if ev is None:
            reason = WaitReason.EV_UNKNOWN
        elif ev <= 0:
            reason = WaitReason.EV_NOT_POSITIVE
        elif entry_id in cycle.pending_decisions:
            reason = WaitReason.DUPLICATE
        if p is not None and forecast is not None:
            confidence = forecast.probability

    def made(created: DecisionCreated, intent: TradeIntent | None) -> Decision:
        return Decision(created, intent, now, ctx.mode, cycle.cycle_id)

    if reason is not None:
        return made(DecisionCreated(
            decision_id=stable_id("dec", "WAIT", reason, basis), action=DecisionAction.WAIT,
            strategy_id=chosen.strategy_id if chosen else None, side=None,
            expected_value_usd=ev, confidence=confidence, reason_codes=[reason],
            evidence_refs=evidence_refs, policy_version=policy_version,
        ), None)  # fmt: skip

    assert chosen is not None and direction is not None and ev is not None
    side = _side(direction)
    created = DecisionCreated(
        decision_id=entry_id, action=DecisionAction.ENTER, strategy_id=chosen.strategy_id,
        side=side, expected_value_usd=ev, confidence=confidence,
        reason_codes=["EV_POSITIVE", f"VOTES_{agree}_AGAINST_{against}"],
        evidence_refs=evidence_refs, policy_version=policy_version,
    )  # fmt: skip
    intent = TradeIntent(
        intent_id=stable_id("int", entry_id), decision_id=entry_id, action=IntentAction.OPEN,
        side=side, symbol=symbol, requested_lot=ctx.initial_lot,
        strategy_id=chosen.strategy_id, cycle_id=cycle.cycle_id, mode=ctx.mode,
        expected_value_usd=ev, prediction_ids=[forecast.prediction_id] if forecast else [],
        valid_until=min(now + INTENT_TTL, chosen.valid_until, ctx.snapshot.valid_until),
    )  # fmt: skip
    return made(created, intent)


def to_events(d: Decision, *, account_id: str, bot_id: str) -> list[EventEnvelope]:
    """`decision.created`, then the `trade.intent` it caused. Ids are deterministic, so
    a republish is ignored by the journal. Mode and cycle come from the decision."""

    def event(kind: str, payload: DecisionCreated | TradeIntent, key: str,
              causation: str | None) -> EventEnvelope:  # fmt: skip
        return make_event(
            kind, source=SOURCE, payload=payload, account_id=account_id, bot_id=bot_id,
            mode=d.mode, correlation_id=d.created.decision_id, causation_id=causation,
            cycle_id=d.cycle_id, event_id=stable_id("evt", key), occurred_at=d.now,
        )  # fmt: skip

    first = event("decision.created", d.created, d.created.decision_id, None)
    if d.intent is None:
        return [first]
    return [first, event("trade.intent", d.intent, d.intent.intent_id, first.event_id)]
