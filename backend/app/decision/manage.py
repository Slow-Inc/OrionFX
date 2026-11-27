"""Exit, scaling and recovery proposals for a cycle that already holds a position (#54;
DEC-05/06/07, UST-12; AT-14, AT-41, AT-42).

Same evidence rules as `decide` (one vote per independent source; the chart counts
as one vote when the live proposals agree). Then:

- EXIT (CLOSE_ALL of the bot's basket) when opposing votes outnumber supporting
  ones. Entry-only gates (lot, cap, cycle P&L, costs, mode) never block an exit,
  and M06 never blocks one either. Only a stale snapshot does: with no fresh evidence
  there is no reversal to act on.
- ADD only as an opportunity:
  - the chart agrees with the held side, below the position cap, the evidence does
    not conflict, and EV after costs is positive;
  - the size is always the owner's initial lot, never sized up;
  - on a losing cycle the reason is RECOVERY_OPPORTUNITY, and that is refused in Live
    (no loss chasing there; M06 refuses it as well);
  - an unknown cycle P&L fails closed.
- Otherwise WAIT: HOLD when the chart points against the position but the
  evidence is not strong enough to exit.

A strategy switch mid-cycle only changes `strategy_id` on the decision. The cycle,
its Profit Target, drawdown and trailing state belong to M06 and are never reset here.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.core.contracts import DecisionCreated, StrategyProposal, TradeIntent
from app.core.enums import DecisionAction, Direction, IntentAction, Mode, Side, WaitReason
from app.core.ids import stable_id
from app.core.time import live
from app.decision.context import DecisionContext
from app.decision.decide import (
    INTENT_TTL,
    Costs,
    CycleView,
    Decision,
    Evidence,
    _ev,
    _live_forecast,
    _probability,
    _votes,
    usable_proposals,
)

EXIT_STRATEGY = "m05.exit"


@dataclass(frozen=True)
class Held:
    """The bot's own open basket in this cycle (never Manual Positions)."""

    side: Side
    lots: Decimal
    positions: int
    cycle_net_usd: Decimal | None
    position_refs: list[str]


def manage(
    ctx: DecisionContext,
    proposals: list[StrategyProposal],
    evidence: Evidence,
    *,
    cycle: CycleView,
    held: Held,
    max_positions: int,
    costs: Costs | None,
    symbol: str,
    policy_version: str,
) -> Decision:
    now = ctx.now
    usable = usable_proposals(proposals, now)
    forecast = _live_forecast(evidence, now)
    v = _votes(evidence, forecast, now)
    directions = {p.direction for p in usable}
    chart = next(iter(directions)) if len(directions) == 1 else None
    votes = v.votes + ([chart] if chart else [])
    held_dir = Direction.UP if held.side is Side.BUY else Direction.DOWN
    support = votes.count(held_dir)
    against = len(votes) - support

    strategy_refs = [f"{p.strategy_id}@{p.strategy_version}" for p in usable]
    evidence_refs = [ctx.snapshot.ref, *strategy_refs, *v.refs]
    cost_basis = None if costs is None else [costs.contract_size, costs.commission_per_lot]
    basis = [evidence_refs, cycle.cycle_id, ctx.mode, symbol, policy_version, ctx.initial_lot,
             ctx.spread, cost_basis, held.side, held.lots, held.positions, held.cycle_net_usd,
             held.position_refs]  # fmt: skip
    chosen = usable[0] if usable else None

    def made(action: DecisionAction, reasons: list[str], intent_action: IntentAction | None,
             ev: Decimal | None = None) -> Decision:  # fmt: skip
        decision_id = stable_id("dec", action, reasons, basis)
        strategy_id = chosen.strategy_id if chosen else None
        created = DecisionCreated(
            decision_id=decision_id, action=action, strategy_id=strategy_id,
            side=held.side if intent_action is IntentAction.ADD else None,
            expected_value_usd=ev,
            confidence=forecast.probability if ev is not None and forecast else None,
            reason_codes=reasons, evidence_refs=evidence_refs, policy_version=policy_version,
        )  # fmt: skip
        intent = None
        if intent_action is not None:
            adding = intent_action is IntentAction.ADD
            intent = TradeIntent(
                intent_id=stable_id("int", decision_id), decision_id=decision_id,
                action=intent_action, side=held.side if adding else None, symbol=symbol,
                requested_lot=ctx.initial_lot if adding else None,
                strategy_id=strategy_id or EXIT_STRATEGY, cycle_id=cycle.cycle_id,
                mode=ctx.mode, expected_value_usd=ev,
                prediction_ids=[forecast.prediction_id] if forecast else [],
                position_refs=held.position_refs,
                valid_until=min([now + INTENT_TTL, ctx.snapshot.valid_until]
                                + ([chosen.valid_until] if adding and chosen else [])),
            )  # fmt: skip
        return Decision(created, intent, now, ctx.mode, cycle.cycle_id)

    def wait(reason: WaitReason, ev: Decimal | None = None) -> Decision:
        return made(DecisionAction.WAIT, [reason], None, ev)

    if not live(ctx.snapshot.available_at, ctx.snapshot.valid_until, now):
        return wait(WaitReason.STALE_INPUT)
    if against > support:
        reasons = ["EVIDENCE_REVERSED", f"VOTES_{against}_AGAINST_{support}"]
        return made(DecisionAction.EXIT, reasons, IntentAction.CLOSE_ALL)
    if ctx.initial_lot <= 0:
        return wait(WaitReason.INVALID_LOT)
    if chosen is None:
        return wait(WaitReason.NO_OPPORTUNITY)
    if chart is None:
        return wait(WaitReason.CONFLICT_STRATEGIES)
    if chart is not held_dir:
        return wait(WaitReason.HOLD)
    if held.positions >= max_positions:
        return wait(WaitReason.CAP_REACHED)
    if held.cycle_net_usd is None:
        return wait(WaitReason.CYCLE_PNL_UNKNOWN)
    losing = held.cycle_net_usd < 0
    if losing and ctx.mode is Mode.LIVE:
        return wait(WaitReason.NO_LIVE_RECOVERY)
    if against >= support:
        return wait(WaitReason.CONFLICT_EVIDENCE)
    ev = _ev(ctx, _probability(forecast, chosen), costs)
    if ev is None:
        return wait(WaitReason.EV_UNKNOWN)
    if ev <= 0:
        return wait(WaitReason.EV_NOT_POSITIVE, ev)
    opportunity = "RECOVERY_OPPORTUNITY" if losing else "OPPORTUNITY"
    add = made(DecisionAction.ADD, [opportunity, f"VOTES_{support}_AGAINST_{against}"],
               IntentAction.ADD, ev)  # fmt: skip
    if add.created.decision_id in cycle.pending_decisions:
        return wait(WaitReason.DUPLICATE, ev)
    return add
