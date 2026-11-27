"""Decision trace and replay (#54; DEC-07, UST-03; AT-14: "rationale reproducible from
stored inputs").

A `DecisionTrace` holds every input `decide`/`manage` read, as one JSON-serialisable
contract. Stored with the decision, it lets an auditor rebuild the same decision
(same id, same reasons, same intent) later. `replay` re-runs the pure functions; it
reads no clock, no store and no broker.
"""

from pydantic import Field

from app.core.contracts import (
    ContractModel,
    FeatureSnapshot,
    LlmReasoning,
    NewsAssessment,
    PredictionGenerated,
    StrategyProposal,
)
from app.core.enums import Mode, Side
from app.core.types import Dec, UtcDatetime
from app.decision.context import DecisionContext
from app.decision.decide import Costs, CycleView, Decision, Evidence, decide
from app.decision.manage import Held, manage


class HeldTrace(ContractModel):
    side: Side
    lots: Dec
    positions: int
    cycle_net_usd: Dec | None
    position_refs: list[str]


class DecisionTrace(ContractModel):
    now: UtcDatetime
    mode: Mode
    snapshot: FeatureSnapshot
    price: Dec | None
    spread: Dec | None
    initial_lot: Dec
    allowed: list[str]
    proposals: list[StrategyProposal]
    forecast: PredictionGenerated | None
    news: list[NewsAssessment]
    reasoning: LlmReasoning | None
    cycle_id: str | None
    open_positions: int
    last_closed_at: UtcDatetime | None
    pending_decisions: list[str]
    contract_size: Dec | None
    commission_per_lot: Dec | None
    symbol: str
    policy_version: str
    held: HeldTrace | None = None
    max_positions: int | None = Field(default=None, gt=0)

    @classmethod
    def capture(
        cls,
        ctx: DecisionContext,
        proposals: list[StrategyProposal],
        evidence: Evidence,
        *,
        cycle: CycleView,
        costs: Costs | None,
        symbol: str,
        policy_version: str,
        held: Held | None,
        max_positions: int | None,
    ) -> "DecisionTrace":
        return cls(
            now=ctx.now, mode=ctx.mode, snapshot=ctx.snapshot, price=ctx.price,
            spread=ctx.spread, initial_lot=ctx.initial_lot, allowed=sorted(ctx.allowed),
            proposals=proposals, forecast=evidence.forecast, news=evidence.news,
            reasoning=evidence.reasoning, cycle_id=cycle.cycle_id,
            open_positions=cycle.open_positions, last_closed_at=cycle.last_closed_at,
            pending_decisions=sorted(cycle.pending_decisions),
            contract_size=costs.contract_size if costs else None,
            commission_per_lot=costs.commission_per_lot if costs else None,
            symbol=symbol, policy_version=policy_version,
            held=None if held is None else HeldTrace(
                side=held.side, lots=held.lots, positions=held.positions,
                cycle_net_usd=held.cycle_net_usd, position_refs=held.position_refs,
            ),
            max_positions=max_positions,
        )  # fmt: skip


def _costs(t: DecisionTrace) -> Costs | None:
    if t.contract_size is None or t.commission_per_lot is None:
        return None
    return Costs(t.contract_size, t.commission_per_lot)


def replay(t: DecisionTrace) -> Decision:
    ctx = DecisionContext(now=t.now, mode=t.mode, snapshot=t.snapshot, price=t.price,
                          spread=t.spread, initial_lot=t.initial_lot,
                          allowed=frozenset(t.allowed))  # fmt: skip
    evidence = Evidence(forecast=t.forecast, news=t.news, reasoning=t.reasoning)
    cycle = CycleView(t.cycle_id, t.open_positions, t.last_closed_at,
                      frozenset(t.pending_decisions))  # fmt: skip
    costs = _costs(t)
    if t.held is None:
        return decide(ctx, t.proposals, evidence, cycle=cycle, costs=costs, symbol=t.symbol,
                      policy_version=t.policy_version)  # fmt: skip
    h = t.held
    held = Held(h.side, h.lots, h.positions, h.cycle_net_usd, h.position_refs)
    return manage(ctx, t.proposals, evidence, cycle=cycle, held=held,
                  max_positions=t.max_positions or 1, costs=costs, symbol=t.symbol,
                  policy_version=t.policy_version)  # fmt: skip
