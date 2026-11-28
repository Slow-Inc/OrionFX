"""MVP rule strategies CAT-S01-01..06 (#52; DEC-03, UST-01, UST-02).

Each strategy is a documented, deterministic rule over the M02 feature snapshot
(plus current price and spread). It proposes a direction for a fresh entry or
nothing; it never sizes beyond the owner's initial lot and never touches the
broker. A required feature missing means no proposal. Thresholds are
[PROPOSED] (master plan: strategy thresholds TBD). Each strategy's artifact
hash covers its own rule source and thresholds (newline-normalised), so editing
one rule changes only that strategy's hash and invalidates only its approval.
Machine Learning Trading (CAT-S01-07) is blocked: it needs a trained model.

- trend_following: H1 direction == M15 structure (non-zero) and H1 ADX >= 25.
- breakout: price above M15 resistance (UP) or below M15 support (DOWN), outside
  a range regime; only the level being tested is needed.
- pullback: H1 trend (ADX >= 20) with M5 RSI pulled back (<= 40 in an uptrend,
  >= 60 in a downtrend).
- range: M15 range regime; price within 0.25 x M15 ATR either side of support
  (UP) or of resistance (DOWN), never beyond the band (that is a breakout).
- scalping: M5 direction == M15 direction (non-zero) and spread <= 0.30; M5
  horizon.
- hybrid: among the members the gate allowed, at least two agree on a direction
  and outnumber the opposing votes; it records the agreeing members and versions as
  its composition, and for an entry `drop_outvoted` drops the outvoted members.
"""

import inspect
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from app.core.contracts import StrategyManifest, StrategyProposal, sha256_ref
from app.core.enums import Direction, IntentAction, Mode, Timeframe
from app.core.ids import new_id
from app.decision.context import DecisionContext
from app.market_data.features import ATR_M15, feature_key
from app.prediction.card import unique_leader

D = Decimal
VERSION = "1.0.0"
HYBRID_VERSION = "1.1.0"
"""1.1.0 (#121): records its composition; its outvoted members are dropped for entries.
One version names one behaviour (AT-41), so the changed Hybrid is re-qualified under a
new version; an approval of hybrid@1.0.0 never covers it."""
H1, M15, M5 = Timeframe.H1, Timeframe.M15, Timeframe.M5

DIR_H1 = feature_key(H1, "CAT-T03-07", "direction")
DIR_M15 = feature_key(M15, "CAT-T03-07", "direction")
DIR_M5 = feature_key(M5, "CAT-T03-07", "direction")
STRUCT_M15 = feature_key(M15, "CAT-T03-04", "structure")
ADX_H1 = feature_key(H1, "CAT-T01-07", "adx")
RANGE_M15 = feature_key(M15, "CAT-T03-06", "range")
RSI_M5 = feature_key(M5, "CAT-T01-04", "rsi")
SUPPORT_M15 = feature_key(M15, "CAT-T03-03", "support")
RESIST_M15 = feature_key(M15, "CAT-T03-03", "resistance")

TREND_ADX, PULLBACK_ADX = D(25), D(20)
PULLBACK_LOW, PULLBACK_HIGH = D(40), D(60)
RANGE_EDGE_ATR = D("0.25")
SCALP_MAX_SPREAD = D("0.30")

Rule = Callable[[DecisionContext, dict[str, Decimal]], Direction | None]


def _sign(x: Decimal) -> Direction | None:
    return Direction.UP if x > 0 else Direction.DOWN if x < 0 else None


def _trend(_: DecisionContext, f: dict[str, Decimal]) -> Direction | None:
    if f[ADX_H1] < TREND_ADX or f[DIR_H1] != f[STRUCT_M15]:
        return None
    return _sign(f[DIR_H1])


def _breakout(ctx: DecisionContext, f: dict[str, Decimal]) -> Direction | None:
    if ctx.price is None or f[RANGE_M15] == 1:
        return None
    resistance, support = ctx.value(RESIST_M15), ctx.value(SUPPORT_M15)
    if resistance is not None and ctx.price > resistance:
        return Direction.UP
    if support is not None and ctx.price < support:
        return Direction.DOWN
    return None


def _pullback(_: DecisionContext, f: dict[str, Decimal]) -> Direction | None:
    if f[ADX_H1] < PULLBACK_ADX:
        return None
    trend = _sign(f[DIR_H1])
    if trend is Direction.UP and f[RSI_M5] <= PULLBACK_LOW:
        return Direction.UP
    if trend is Direction.DOWN and f[RSI_M5] >= PULLBACK_HIGH:
        return Direction.DOWN
    return None


def _range(ctx: DecisionContext, f: dict[str, Decimal]) -> Direction | None:
    if ctx.price is None or f[RANGE_M15] != 1:
        return None
    edge = RANGE_EDGE_ATR * f[ATR_M15]
    if abs(ctx.price - f[SUPPORT_M15]) <= edge:
        return Direction.UP
    if abs(ctx.price - f[RESIST_M15]) <= edge:
        return Direction.DOWN
    return None


def _scalp(ctx: DecisionContext, f: dict[str, Decimal]) -> Direction | None:
    if ctx.spread is None or ctx.spread > SCALP_MAX_SPREAD or f[DIR_M5] != f[DIR_M15]:
        return None
    return _sign(f[DIR_M5])


def _rule_hash(
    rule: Callable[..., object], required: tuple[str, ...], *also: Callable[..., object]
) -> str:
    thresholds = (TREND_ADX, PULLBACK_ADX, PULLBACK_LOW, PULLBACK_HIGH, RANGE_EDGE_ATR,
                  SCALP_MAX_SPREAD)  # fmt: skip
    source = "".join(inspect.getsource(f) for f in (rule, *also)).replace("\r\n", "\n")
    return sha256_ref(f"{source}|{required}|{thresholds}".encode())


def _manifest(
    sid: str,
    catalog_id: str,
    tfs: list[Timeframe],
    artifact_hash: str,
    refs: list[str],
    version: str = VERSION,
) -> StrategyManifest:
    return StrategyManifest(
        strategy_id=sid,
        version=version,
        artifact_hash=artifact_hash,
        family="S01",
        catalog_ids=[catalog_id],
        technique_refs=refs,
        timeframes=tfs,
        warmup_bars=0,
        parameters={},
        risk_tags=[],
        supported_modes=[Mode.SHADOW, Mode.DEMO],
    )


def _proposal(
    manifest: StrategyManifest,
    horizon: Timeframe,
    ctx: DecisionContext,
    direction: Direction,
    refs: list[str],
    composition_id: str | None = None,
) -> StrategyProposal:
    rationale = f"{manifest.strategy_id} rule v{manifest.version} -> {direction}"
    return StrategyProposal(
        proposal_id=new_id("prop"),
        strategy_id=manifest.strategy_id,
        strategy_version=manifest.version,
        technique_refs=refs,
        composition_id=composition_id,
        decision_id=None,
        direction=direction,
        action=IntentAction.OPEN,
        horizon=horizon,
        expected_value_usd=None,
        cost_estimate_usd=None,
        requested_lot=ctx.initial_lot,
        valid_until=ctx.snapshot.valid_until,
        rationale=f"{rationale} ({composition_id})" if composition_id else rationale,
        constraints=[],
    )


@dataclass(frozen=True)
class RuleStrategy:
    manifest: StrategyManifest
    required: tuple[str, ...]
    rule: Rule
    horizon: Timeframe

    def direction(self, ctx: DecisionContext) -> Direction | None:
        f: dict[str, Decimal] = {}
        for key in self.required:
            value = ctx.value(key)
            if value is None:
                return None
            f[key] = value
        return self.rule(ctx, f)

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        d = self.direction(ctx)
        return (
            None
            if d is None
            else _proposal(self.manifest, self.horizon, ctx, d, list(self.required))
        )


def _rule(
    sid: str,
    cat: str,
    tfs: list[Timeframe],
    required: tuple[str, ...],
    rule: Rule,
    horizon: Timeframe,
) -> RuleStrategy:
    return RuleStrategy(
        _manifest(sid, cat, tfs, _rule_hash(rule, required), list(required)),
        required,
        rule,
        horizon,
    )


_RULES = [
    _rule("trend_following", "CAT-S01-01", [H1, M15], (DIR_H1, STRUCT_M15, ADX_H1), _trend, H1),
    _rule("scalping", "CAT-S01-02", [M5, M15], (DIR_M5, DIR_M15), _scalp, M5),
    _rule("pullback", "CAT-S01-03", [H1, M15, M5], (DIR_H1, ADX_H1, RSI_M5), _pullback, M15),
    _rule("breakout", "CAT-S01-04", [M15], (RANGE_M15,), _breakout, M15),
    _rule("range", "CAT-S01-05", [M15], (RANGE_M15, ATR_M15, SUPPORT_M15, RESIST_M15), _range, M15),
]


@dataclass(frozen=True)
class Hybrid:
    manifest: StrategyManifest
    members: tuple[RuleStrategy, ...]
    horizon: Timeframe = M15

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        """Votes only from members the gate allowed (`ctx.allowed`)."""
        voters = [m for m in self.members if m.manifest.strategy_id in ctx.allowed]
        votes = [(m, d) for m in voters if (d := m.direction(ctx)) is not None]
        lead = unique_leader({d: D(n) for d, n in Counter(d for _, d in votes).items()})
        if lead is None or lead[1] < 2:
            return None
        refs = list(dict.fromkeys(k for m, d in votes if d is lead[0] for k in m.required))
        agree = sorted(f"{m.manifest.strategy_id}@{m.manifest.version}"
                       for m, d in votes if d is lead[0])  # fmt: skip
        composition = f"{self.manifest.strategy_id}:{'+'.join(agree)}"  # exact versions, AT-41
        return _proposal(self.manifest, self.horizon, ctx, lead[0], refs, composition)


# For an entry only (#121): when a Hybrid proposes, drop its members' proposals on the
# other side. They would always conflict with the majority, so Hybrid could never win a
# 2 vs 1. Agreeing members stay (one may match the forecast's horizon). Never used while
# managing a basket (`fusion.gather` applies it only without a held side): there a split
# chart must stay no vote, or an exit could weaken. This is a comment, not a docstring,
# so the Hybrid hash covers only the code that decides behaviour.
def drop_outvoted(proposals: list[StrategyProposal]) -> list[StrategyProposal]:
    kept = proposals
    for h in proposals:
        s = STRATEGIES.get(h.strategy_id)
        if isinstance(s, Hybrid):
            members = {m.manifest.strategy_id for m in s.members}
            kept = [p for p in kept if p.strategy_id not in members or p.direction is h.direction]
    return kept


# Hybrid's behaviour is its vote and the entry-time dropping rule: both are hashed. Where
# the rule applies (entries only, in `fusion.gather`) is decision policy, versioned by
# `ProtectedPolicy.policy_version`, not by this hash.
_HYBRID_HASH = _rule_hash(
    Hybrid.propose, tuple(m.manifest.strategy_id for m in _RULES), drop_outvoted
)
STRATEGIES: dict[str, RuleStrategy | Hybrid] = {s.manifest.strategy_id: s for s in _RULES}
STRATEGIES["hybrid"] = Hybrid(
    _manifest("hybrid", "CAT-S01-06", [H1, M15, M5], _HYBRID_HASH, [], HYBRID_VERSION),
    tuple(_RULES),
)


def catalog_ids() -> set[str]:
    return {cid for s in STRATEGIES.values() for cid in s.manifest.catalog_ids}
