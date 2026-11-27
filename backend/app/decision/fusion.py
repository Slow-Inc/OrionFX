"""The shared M05 core: evidence fusion, expected value, decision and intent building,
events (#53, #54; DEC-01/02/04/05/07, AT-14, AT-18, AT-19, AT-34).

`decide` (no position yet) and `manage` (the bot already holds a basket) are two
policies on top of this module. Rules, in one place:

Evidence votes once per independent source:
- the forecast (one vote: the M04 ensemble already merged its members), when live
  and of usable data quality;
- live news assessments that cite a source, as one impact-weighted vote, unless the
  forecast is NEWS_ENHANCED (it already contains them);
- an LLM reasoning, only while live (available and unexpired at `now`), only if it
  restates nothing already voting (no voting or forecast-covered news, not the
  forecast), and only if every news item it cites is live and cited; otherwise it is
  ignored and not referenced;
- the chart: one vote when the live OPEN proposals agree on a direction.
Stale or uncited evidence is ignored and not referenced.

EV = move x (2p - 1) - (spread x size + 2 x commission per lot x lot), where
size = lot x contract size and move = TARGET_ATR x M15 ATR x size. p is the
forecast's calibrated probability, used only when the forecast agrees, has OK data
and the trade's horizon. Thresholds are [PROPOSED].

Ids are `stable_id`s of the action, reasons and inputs, so republishing a decision
is ignored by the journal. Repeats across ticks, where floating values such as
spread move, are refused by action: `CycleView.pending` lists the intent actions
M06 has not yet resolved for the cycle.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.contracts import (
    Confidence,
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
    Timeframe,
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
NEWS_VETO_IMPACT = D("0.6")
"""Net opposing news impact at or above which an entry is refused (#115) [PROPOSED]."""
INTENT_TTL = timedelta(seconds=30)
SOURCE = "m05"
EXIT_STRATEGY = "m05.exit"


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
    pending: frozenset[IntentAction] = frozenset()
    """Intent actions published for this cycle that M06 has not resolved yet."""


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
    direction: Direction | None = None
    """The side it bet on: the agreed chart side when entering, the held side when
    holding or adding, against the held side for an EXIT; None when it leaned nowhere."""
    horizon: Timeframe | None = None
    """The horizon of a usable proposal on that side, if any."""


@dataclass(frozen=True)
class Inputs:
    """Everything a policy reads, gathered once."""

    usable: list[StrategyProposal]
    chosen: StrategyProposal | None
    chart: Direction | None
    forecast: PredictionGenerated | None
    votes: list[Direction]
    """Evidence votes, without the chart."""
    evidence_refs: list[str]
    basis: list[object]
    leaning: Direction | None = None
    """The side a decision leans to before its action: the held basket's when managing,
    else the side the chart proposals agree on (None when they conflict or are absent)."""

    def tally(self, direction: Direction, *, with_chart: bool = True) -> tuple[int, int]:
        """(support, against) for `direction`."""
        votes = self.votes + ([self.chart] if with_chart and self.chart else [])
        support = votes.count(direction)
        return support, len(votes) - support


def side_of(d: Direction) -> Side:
    if d is Direction.NEUTRAL:
        raise ValueError("a NEUTRAL direction has no side")
    return Side.BUY if d is Direction.UP else Side.SELL


def direction_of(s: Side) -> Direction:
    return Direction.UP if s is Side.BUY else Direction.DOWN


def snapshot_live(ctx: DecisionContext) -> bool:
    return live(ctx.snapshot.available_at, ctx.snapshot.valid_until, ctx.now)


def usable_proposals(proposals: list[StrategyProposal], now: datetime) -> list[StrategyProposal]:
    """Live OPEN proposals with a direction, in a deterministic order."""
    return sorted(
        (p for p in proposals if p.action is IntentAction.OPEN
         and p.direction is not Direction.NEUTRAL and now < p.valid_until),
        key=lambda p: p.strategy_id,
    )  # fmt: skip


def _signed(d: Direction, x: Decimal) -> Decimal:
    return x if d is Direction.UP else -x if d is Direction.DOWN else D(0)


def news_veto(ev: Evidence, side: Direction, now: datetime) -> bool:
    """Whether news vetoes an entry on `side` (#115, PRD #114).

    Live, source-cited assessments are netted by impact, exactly as the news vote is
    (NEUTRAL adds nothing); the entry is refused if the net opposes `side` with
    magnitude >= NEWS_VETO_IMPACT. The same news is already cited in the decision's
    evidence refs. A guard, not a vote: it can only turn an entry into WAIT, it ignores
    the forecast's lineage, it never touches exits, and no news means no veto.
    """
    net = sum(
        (_signed(n.direction, n.impact) for n in ev.news
         if n.source_refs and live(n.available_at, n.valid_until, now)),
        D(0),
    )  # fmt: skip
    against = -net if side is Direction.UP else net if side is Direction.DOWN else D(0)
    return against >= NEWS_VETO_IMPACT


def _live_forecast(ev: Evidence, now: datetime) -> PredictionGenerated | None:
    f = ev.forecast
    if f is None or f.data_quality not in USABLE_QUALITY:
        return None
    return f if live(f.generated_at, f.valid_until, now) else None


def _votes(
    ev: Evidence, forecast: PredictionGenerated | None, now: datetime
) -> tuple[list[Direction], list[str]]:
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
    if (
        r is not None
        and live(r.available_at, r.valid_until, now)
        and (set(r.news_assessment_ids) <= news_ids)
    ):
        refs.append(r.reasoning_id)
        restates = already_voting.intersection(r.news_assessment_ids) or (
            forecast is not None and forecast.prediction_id in r.prediction_ids
        )
        if r.direction is not Direction.NEUTRAL and not restates:
            votes.append(r.direction)
    return votes, refs


def gather(
    ctx: DecisionContext,
    proposals: list[StrategyProposal],
    evidence: Evidence,
    *,
    cycle: CycleView,
    costs: Costs | None,
    symbol: str,
    policy_version: str,
    held: Side | None = None,
) -> Inputs:
    usable = usable_proposals(proposals, ctx.now)
    directions = {p.direction for p in usable}
    forecast = _live_forecast(evidence, ctx.now)
    votes, refs = _votes(evidence, forecast, ctx.now)
    strategy_refs = [f"{p.strategy_id}@{p.strategy_version}" for p in usable]
    evidence_refs = [ctx.snapshot.ref, *strategy_refs, *refs]
    cost_basis = None if costs is None else [costs.contract_size, costs.commission_per_lot]
    basis: list[object] = [evidence_refs, cycle.cycle_id, ctx.mode, symbol, policy_version,
                           ctx.initial_lot, ctx.spread, cost_basis]  # fmt: skip
    chart = next(iter(directions)) if len(directions) == 1 else None
    return Inputs(
        usable=usable,
        chosen=usable[0] if usable else None,
        chart=chart,
        forecast=forecast,
        votes=votes,
        evidence_refs=evidence_refs,
        basis=basis,
        leaning=direction_of(held) if held else chart,
    )


def expected_value(
    ctx: DecisionContext, inp: Inputs, costs: Costs | None
) -> tuple[Decimal | None, Confidence | None]:
    """EV for adding the initial lot in the chosen proposal's direction, and the
    probability it rests on; (None, None) when any input is missing or unusable."""
    f, chosen, atr = inp.forecast, inp.chosen, ctx.value(ATR_M15)
    if (
        f is None
        or chosen is None
        or f.direction is not chosen.direction
        or not f.probability.calibrated
        or f.data_quality is not DataQuality.OK
        or f.horizon is not chosen.horizon
        or costs is None
        or ctx.spread is None
        or atr is None
    ):
        return None, None
    p = f.probability.value
    size = ctx.initial_lot * costs.contract_size
    cost = ctx.spread * size + 2 * costs.commission_per_lot * ctx.initial_lot
    return (TARGET_ATR * atr * size * (2 * p - 1) - cost).quantize(CENT), f.probability


@dataclass(frozen=True)
class Order:
    """What a non-WAIT decision asks M06 for."""

    action: IntentAction
    side: Side | None
    lot: Decimal | None
    position_refs: list[str] = field(default_factory=list)


def build(
    ctx: DecisionContext,
    cycle: CycleView,
    inp: Inputs,
    *,
    action: DecisionAction,
    reasons: list[str],
    symbol: str,
    policy_version: str,
    ev: Decimal | None = None,
    confidence: Confidence | None = None,
    order: Order | None = None,
    extra_basis: list[object] | None = None,
) -> Decision:
    decision_id = stable_id("dec", action, reasons, inp.basis, extra_basis or [])
    chosen = inp.chosen
    created = DecisionCreated(
        decision_id=decision_id, action=action,
        strategy_id=chosen.strategy_id if chosen else None,
        side=order.side if order else None, expected_value_usd=ev, confidence=confidence,
        reason_codes=reasons, evidence_refs=inp.evidence_refs, policy_version=policy_version,
    )  # fmt: skip
    intent = None
    if order is not None:
        bounds = [ctx.now + INTENT_TTL, ctx.snapshot.valid_until]
        if order.side is not None and chosen is not None:
            bounds.append(chosen.valid_until)
        intent = TradeIntent(
            intent_id=stable_id("int", decision_id), decision_id=decision_id,
            action=order.action, side=order.side, symbol=symbol, requested_lot=order.lot,
            strategy_id=chosen.strategy_id if chosen else EXIT_STRATEGY,
            cycle_id=cycle.cycle_id, mode=ctx.mode, expected_value_usd=ev,
            prediction_ids=[inp.forecast.prediction_id] if inp.forecast else [],
            position_refs=order.position_refs, valid_until=min(bounds),
        )  # fmt: skip
    # what M07 scores: an EXIT bets against the held side; the horizon is that of a
    # proposal on the same side, if any (never an opposing proposal's)
    direction = inp.leaning
    if action is DecisionAction.EXIT and direction is not None:
        direction = Direction.DOWN if direction is Direction.UP else Direction.UP
    same_side = [p for p in inp.usable if p.direction is direction]
    horizon = same_side[0].horizon if direction is not None and same_side else None
    return Decision(created, intent, ctx.now, ctx.mode, cycle.cycle_id, direction, horizon)


def wait(
    ctx: DecisionContext,
    cycle: CycleView,
    inp: Inputs,
    reason: WaitReason,
    *,
    symbol: str,
    policy_version: str,
    ev: Decimal | None = None,
    confidence: Confidence | None = None,
    extra_basis: list[object] | None = None,
) -> Decision:
    return build(ctx, cycle, inp, action=DecisionAction.WAIT, reasons=[reason], symbol=symbol,
                 policy_version=policy_version, ev=ev, confidence=confidence,
                 extra_basis=extra_basis)  # fmt: skip


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
