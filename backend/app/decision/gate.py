"""Which strategies may propose now (#52; DEC-03, UST-06, UST-08; AT-32).

A strategy passes only if the owner enabled it, its newest registry eligibility
record approves the current mode and has not expired at `now`, and the feature
snapshot is usable (M02's rule). Every refusal is reported with its reason.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from app.core.contracts import StrategyManifest, StrategyProposal
from app.decision.context import DecisionContext
from app.market_data.features import snapshot_problem
from app.storage.registry import ArtifactRegistry, subject_ref


class Strategy(Protocol):
    @property
    def manifest(self) -> StrategyManifest: ...

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None: ...


@dataclass(frozen=True)
class GateResult:
    allowed: list[Strategy]
    refused: dict[str, str] = field(default_factory=dict)


def eligible(
    strategies: Iterable[Strategy],
    ctx: DecisionContext,
    *,
    enabled: Iterable[str],
    registry: ArtifactRegistry,
) -> GateResult:
    on = set(enabled)
    problem = snapshot_problem(ctx.snapshot, ctx.now)
    allowed: list[Strategy] = []
    refused: dict[str, str] = {}
    for s in strategies:
        m = s.manifest
        if m.strategy_id not in on:
            refused[m.strategy_id] = "not enabled by the owner"
            continue
        record = registry.eligibility("strategy", subject_ref(m.strategy_id, m.version))
        expired = (
            record is not None and record.expires_at is not None and record.expires_at <= ctx.now
        )
        if record is None or expired or ctx.mode not in record.approved_modes:
            refused[m.strategy_id] = f"not approved for {ctx.mode}"
            continue
        if problem is not None:
            refused[m.strategy_id] = problem
            continue
        allowed.append(s)
    return GateResult(allowed, refused)
