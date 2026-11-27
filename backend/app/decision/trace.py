"""Decision trace and replay (#54; DEC-07, UST-03; AT-14: "rationale reproducible from
stored inputs").

A `DecisionTrace` holds every input `decide`/`manage` read, nested the same way the
inputs are, as one JSON-serialisable contract. Stored with the decision, it lets an
auditor rebuild the same decision (same id, reasons and intent) later. `replay`
re-runs the pure policies; it reads no clock, no store and no broker. A test checks
that every input field is captured, so a new field cannot be silently dropped.
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
from app.core.enums import IntentAction, Mode, Side
from app.core.types import Dec, UtcDatetime
from app.decision.context import DecisionContext
from app.decision.decide import decide
from app.decision.fusion import Costs, CycleView, Decision, Evidence
from app.decision.manage import Held, manage


class ContextTrace(ContractModel):
    now: UtcDatetime
    mode: Mode
    snapshot: FeatureSnapshot
    price: Dec | None
    spread: Dec | None
    initial_lot: Dec
    allowed: list[str]


class EvidenceTrace(ContractModel):
    forecast: PredictionGenerated | None
    news: list[NewsAssessment]
    reasoning: LlmReasoning | None


class CycleTrace(ContractModel):
    cycle_id: str | None
    open_positions: int
    last_closed_at: UtcDatetime | None
    pending: list[IntentAction]


class CostsTrace(ContractModel):
    contract_size: Dec
    commission_per_lot: Dec


class HeldTrace(ContractModel):
    side: Side
    positions: int
    cycle_net_usd: Dec | None
    position_refs: list[str]


class ManageTrace(ContractModel):
    """Present iff the decision came from `manage`; `max_positions` is then required."""

    held: HeldTrace
    max_positions: int = Field(ge=0)


class DecisionTrace(ContractModel):
    ctx: ContextTrace
    proposals: list[StrategyProposal]
    evidence: EvidenceTrace
    cycle: CycleTrace
    costs: CostsTrace | None
    symbol: str
    policy_version: str
    manage: ManageTrace | None = None

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
        held: Held | None = None,
        max_positions: int | None = None,
    ) -> "DecisionTrace":
        if (held is None) != (max_positions is None):
            raise ValueError("held and max_positions go together (manage) or not at all")
        managed = None
        if held is not None and max_positions is not None:
            managed = ManageTrace(held=HeldTrace(**vars(held)), max_positions=max_positions)
        return cls(
            ctx=ContextTrace(now=ctx.now, mode=ctx.mode, snapshot=ctx.snapshot, price=ctx.price,
                             spread=ctx.spread, initial_lot=ctx.initial_lot,
                             allowed=sorted(ctx.allowed)),
            proposals=proposals,
            evidence=EvidenceTrace(forecast=evidence.forecast, news=evidence.news,
                                   reasoning=evidence.reasoning),
            cycle=CycleTrace(cycle_id=cycle.cycle_id, open_positions=cycle.open_positions,
                             last_closed_at=cycle.last_closed_at, pending=sorted(cycle.pending)),
            costs=None if costs is None else CostsTrace(**vars(costs)),
            symbol=symbol, policy_version=policy_version, manage=managed,
        )  # fmt: skip


def replay(t: DecisionTrace) -> Decision:
    c = t.ctx
    ctx = DecisionContext(now=c.now, mode=c.mode, snapshot=c.snapshot, price=c.price,
                          spread=c.spread, initial_lot=c.initial_lot,
                          allowed=frozenset(c.allowed))  # fmt: skip
    evidence = Evidence(t.evidence.forecast, t.evidence.news, t.evidence.reasoning)
    cy = t.cycle
    cycle = CycleView(cy.cycle_id, cy.open_positions, cy.last_closed_at, frozenset(cy.pending))
    costs = None if t.costs is None else Costs(t.costs.contract_size, t.costs.commission_per_lot)
    if t.manage is None:
        return decide(ctx, t.proposals, evidence, cycle=cycle, costs=costs, symbol=t.symbol,
                      policy_version=t.policy_version)  # fmt: skip
    h = t.manage.held
    held = Held(h.side, h.positions, h.cycle_net_usd, h.position_refs)
    return manage(ctx, t.proposals, evidence, cycle=cycle, held=held,
                  max_positions=t.manage.max_positions, costs=costs, symbol=t.symbol,
                  policy_version=t.policy_version)  # fmt: skip
