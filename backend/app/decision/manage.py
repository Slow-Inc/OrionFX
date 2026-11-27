"""Exit, scaling and recovery proposals for a cycle that already holds the bot's basket
(#54; DEC-05/06/07, UST-12; AT-14, AT-41, AT-42). Fusion, EV and building live in
`fusion`.

- EXIT (CLOSE_ALL of the bot's basket) when opposing votes outnumber supporting ones.
  - Entry-only gates (lot, cap, Cycle Net, costs, mode) never block an exit, and
    M06 never blocks one either.
  - With a stale feature snapshot the chart vote is dropped, but live forecast and
    news can still call an exit.
  - While a CLOSE_ALL is pending in M06, no new one is proposed (EXIT_PENDING).
- ADD only as an opportunity:
  - the chart agrees with the held side, below the position cap, the evidence does
    not conflict, and EV after costs is positive;
  - the size is always the owner's initial lot, never sized up;
  - on a losing cycle the reason is RECOVERY_OPPORTUNITY, and that is refused in Live.
    M05 is the only layer refusing a same-lot Live recovery add: M06's veto refuses
    only a size-up on a losing cycle (MARTINGALE_BEHAVIOR). Whether M06 should also
    refuse same-lot adds is an open question (docs/OPEN-WORK-LEDGER.md);
  - an unknown Cycle Net fails closed, and a pending ADD blocks another (DUPLICATE).
- Otherwise WAIT: HOLD when the chart points against the position but the evidence
  is not strong enough to exit.

A strategy switch mid-cycle only changes `strategy_id` on the decision. The cycle,
its Profit Target, drawdown and trailing state belong to M06 and are never reset here.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.core.contracts import StrategyProposal
from app.core.enums import DecisionAction, IntentAction, Mode, Side, WaitReason
from app.decision.context import DecisionContext
from app.decision.fusion import (
    Costs,
    CycleView,
    Decision,
    Evidence,
    Order,
    build,
    direction_of,
    expected_value,
    gather,
    snapshot_live,
    wait,
)


@dataclass(frozen=True)
class Held:
    """The bot's own open basket in this cycle (never Manual Positions)."""

    side: Side
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
    inp = gather(ctx, proposals, evidence, cycle=cycle, costs=costs, symbol=symbol,
                 policy_version=policy_version)  # fmt: skip
    extra: list[object] = [held.side, held.positions, held.position_refs]

    held_dir = direction_of(held.side)  # every managed decision leans to the basket

    def waiting(reason: WaitReason, **more: object) -> Decision:
        return wait(ctx, cycle, inp, reason, symbol=symbol, policy_version=policy_version,
                    extra_basis=extra, leaning=held_dir, **more)  # type: ignore[arg-type]  # fmt: skip

    fresh = snapshot_live(ctx)
    support, against = inp.tally(held_dir, with_chart=fresh)
    if against > support:
        if IntentAction.CLOSE_ALL in cycle.pending:
            return waiting(WaitReason.EXIT_PENDING)
        return build(ctx, cycle, inp, action=DecisionAction.EXIT,
                     reasons=["EVIDENCE_REVERSED", f"VOTES_{against}_AGAINST_{support}"],
                     symbol=symbol, policy_version=policy_version, extra_basis=extra,
                     leaning=held_dir, order=Order(IntentAction.CLOSE_ALL, None, None,
                                 held.position_refs))  # fmt: skip
    if not fresh:
        return waiting(WaitReason.STALE_INPUT)
    if ctx.initial_lot <= 0:
        return waiting(WaitReason.INVALID_LOT)
    if inp.chosen is None:
        return waiting(WaitReason.NO_OPPORTUNITY)
    if inp.chart is None:
        return waiting(WaitReason.CONFLICT_STRATEGIES)
    if inp.chart is not held_dir:
        return waiting(WaitReason.HOLD)
    if held.positions >= max_positions:
        return waiting(WaitReason.CAP_REACHED)
    if held.cycle_net_usd is None:
        return waiting(WaitReason.CYCLE_NET_UNKNOWN)
    losing = held.cycle_net_usd < 0
    if losing and ctx.mode is Mode.LIVE:
        return waiting(WaitReason.NO_LIVE_RECOVERY)
    if against >= support:
        return waiting(WaitReason.CONFLICT_EVIDENCE)
    ev, confidence = expected_value(ctx, inp, costs)
    if ev is None:
        return waiting(WaitReason.EV_UNKNOWN)
    if ev <= 0:
        return waiting(WaitReason.EV_NOT_POSITIVE, ev=ev, confidence=confidence)
    if IntentAction.ADD in cycle.pending:
        return waiting(WaitReason.DUPLICATE, ev=ev, confidence=confidence)
    opportunity = "RECOVERY_OPPORTUNITY" if losing else "OPPORTUNITY"
    return build(ctx, cycle, inp, action=DecisionAction.ADD,
                 reasons=[opportunity, f"VOTES_{support}_AGAINST_{against}"], symbol=symbol,
                 policy_version=policy_version, ev=ev, confidence=confidence,
                 extra_basis=extra, leaning=held_dir,
                 order=Order(IntentAction.ADD, held.side, ctx.initial_lot,
                             held.position_refs))  # fmt: skip
