"""Entry decisions: ENTER or WAIT when the cycle holds no position (#53; DEC-01/02/04/05,
UST-03; AT-14, AT-18, AT-19, AT-34). Fusion, EV and building live in `fusion`.

`decide` is pure. It emits a `decision.created` and, only for ENTER, a `trade.intent`:
a proposal M06 vetoes or allows, never a broker order. WAIT is the default
(`WaitReason`, checked in this order):
- STALE_INPUT: the feature snapshot is not live at `now`.
- INVALID_LOT: the owner's initial lot is not positive.
- NO_OPPORTUNITY: no live OPEN proposal with a direction.
- CONFLICT_STRATEGIES: the proposing strategies disagree.
- POSITION_OPEN: the cycle already holds a position (`manage` handles it).
- COOLDOWN: a cycle closed less than COOLDOWN ago.
- CONFLICT_EVIDENCE: opposing votes >= agreeing votes (the chart counts as one).
- EV_UNKNOWN: no usable probability for the trade, or a cost input / the ATR missing.
- EV_NOT_POSITIVE: EV after spread and round-trip commission is <= 0.
- DUPLICATE: an OPEN intent for this cycle is still pending in M06.
EV is computed only once every gate before it passed.
"""

from datetime import timedelta

from app.core.contracts import StrategyProposal
from app.core.enums import DecisionAction, IntentAction, WaitReason
from app.decision.context import DecisionContext
from app.decision.fusion import (
    Costs,
    CycleView,
    Decision,
    Evidence,
    Order,
    build,
    expected_value,
    gather,
    side_of,
    snapshot_live,
    wait,
)

COOLDOWN = timedelta(minutes=15)


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
    inp = gather(ctx, proposals, evidence, cycle=cycle, costs=costs, symbol=symbol,
                 policy_version=policy_version)  # fmt: skip

    def waiting(reason: WaitReason, **extra: object) -> Decision:
        return wait(ctx, cycle, inp, reason, symbol=symbol, policy_version=policy_version,
                    **extra)  # type: ignore[arg-type]  # fmt: skip

    if not snapshot_live(ctx):
        return waiting(WaitReason.STALE_INPUT)
    if ctx.initial_lot <= 0:
        return waiting(WaitReason.INVALID_LOT)
    if inp.chosen is None:
        return waiting(WaitReason.NO_OPPORTUNITY)
    if inp.chart is None:
        return waiting(WaitReason.CONFLICT_STRATEGIES)
    if cycle.open_positions > 0:
        return waiting(WaitReason.POSITION_OPEN)
    if cycle.last_closed_at is not None and ctx.now - cycle.last_closed_at < COOLDOWN:
        return waiting(WaitReason.COOLDOWN)
    agree, against = inp.tally(inp.chart)
    if against >= agree:
        return waiting(WaitReason.CONFLICT_EVIDENCE)
    ev, confidence = expected_value(ctx, inp, costs)
    if ev is None:
        return waiting(WaitReason.EV_UNKNOWN)
    if ev <= 0:
        return waiting(WaitReason.EV_NOT_POSITIVE, ev=ev, confidence=confidence)
    if IntentAction.OPEN in cycle.pending:
        return waiting(WaitReason.DUPLICATE, ev=ev, confidence=confidence)
    order = Order(IntentAction.OPEN, side_of(inp.chart), ctx.initial_lot)
    return build(ctx, cycle, inp, action=DecisionAction.ENTER,
                 reasons=["EV_POSITIVE", f"VOTES_{agree}_AGAINST_{against}"], symbol=symbol,
                 policy_version=policy_version, ev=ev, confidence=confidence,
                 order=order)  # fmt: skip
