"""Which strategies may propose now (#52; DEC-03, UST-06, UST-08; AT-32).

A strategy passes only if:
- the owner listed it in `strategies_enabled` (the config toggle) AND the
  registry record is user-enabled: both consents must hold, deliberately;
- the record current at `now` (point-in-time) approves the mode at `now`;
- that record approves this exact code: its hash equals the strategy's hash;
- the feature snapshot is usable (M02's rule).
Every refusal is reported with its reason. `with_allowed` hands the decision
loop a context that tells composite strategies which members passed.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
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

    def with_allowed(self, ctx: DecisionContext) -> DecisionContext:
        return replace(ctx, allowed=frozenset(s.manifest.strategy_id for s in self.allowed))


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
        ref = subject_ref(m.strategy_id, m.version)
        record = registry.eligibility_at("strategy", ref, ctx.now)
        if record is None or not record.approves(ctx.mode, ctx.now):
            refused[m.strategy_id] = f"not approved for {ctx.mode}"
            continue
        if record.subject_hash != m.artifact_hash:
            refused[m.strategy_id] = "approval is for different code (hash changed)"
            continue
        if problem is not None:
            refused[m.strategy_id] = problem
            continue
        allowed.append(s)
    return GateResult(allowed, refused)
