"""MVP rule strategies CAT-S01-01..06 (#52; DEC-03, UST-01, UST-02).

Each strategy is a documented, deterministic rule over the M02 feature snapshot
(plus current price and spread). It proposes a direction for a fresh entry or
nothing; it never sizes beyond the owner's initial lot and never touches the
broker. Any required feature missing means no proposal. Thresholds are
[PROPOSED] (master plan: strategy thresholds TBD) and part of each version.
Machine Learning Trading (CAT-S01-07) is blocked: it needs a trained model.

- trend_following: H1 direction == M15 structure (non-zero) and H1 ADX >= 25.
- breakout: price beyond the nearest M15 resistance (UP) or support (DOWN),
  outside a range regime.
- pullback: H1 trend (ADX >= 20) with M5 RSI pulled back (<= 40 in an uptrend,
  >= 60 in a downtrend).
- range: M15 range regime; price within 0.25 x M15 ATR of support (UP) or of
  resistance (DOWN).
- scalping: M5 direction == M15 direction (non-zero) and spread <= 0.30; M5
  horizon.
- hybrid: at least two of the above agree on a direction.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.core.contracts import StrategyManifest, StrategyProposal
from app.core.enums import Direction, IntentAction, Mode, Timeframe
from app.core.ids import new_id
from app.decision.context import DecisionContext
from app.market_data.features import feature_key

D = Decimal
VERSION = "1.0.0"
ARTIFACT_HASH = "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
H1, M15, M5 = Timeframe.H1, Timeframe.M15, Timeframe.M5

DIR_H1 = feature_key(H1, "CAT-T03-07", "direction")
DIR_M15 = feature_key(M15, "CAT-T03-07", "direction")
DIR_M5 = feature_key(M5, "CAT-T03-07", "direction")
STRUCT_M15 = feature_key(M15, "CAT-T03-04", "structure")
ADX_H1 = feature_key(H1, "CAT-T01-07", "adx")
RANGE_M15 = feature_key(M15, "CAT-T03-06", "range")
RSI_M5 = feature_key(M5, "CAT-T01-04", "rsi")
ATR_M15 = feature_key(M15, "CAT-T01-06", "atr")
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
    if ctx.price > f[RESIST_M15]:
        return Direction.UP
    if ctx.price < f[SUPPORT_M15]:
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
    if ctx.price <= f[SUPPORT_M15] + edge:
        return Direction.UP
    if ctx.price >= f[RESIST_M15] - edge:
        return Direction.DOWN
    return None


def _scalp(ctx: DecisionContext, f: dict[str, Decimal]) -> Direction | None:
    if ctx.spread is None or ctx.spread > SCALP_MAX_SPREAD or f[DIR_M5] != f[DIR_M15]:
        return None
    return _sign(f[DIR_M5])


@dataclass(frozen=True)
class RuleStrategy:
    manifest: StrategyManifest
    required: tuple[str, ...]
    rule: Rule
    horizon: Timeframe

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        values = {key: ctx.value(key) for key in self.required}
        if any(v is None for v in values.values()):
            return None
        direction = self.rule(ctx, {key: v for key, v in values.items() if v is not None})
        if direction is None:
            return None
        return _proposal(self, ctx, direction)


def _proposal(
    s: "RuleStrategy | Hybrid", ctx: DecisionContext, direction: Direction
) -> StrategyProposal:
    pid = new_id("prop")
    return StrategyProposal(
        proposal_id=pid,
        strategy_id=s.manifest.strategy_id,
        strategy_version=s.manifest.version,
        technique_refs=list(s.required),
        composition_id=None,
        decision_id=pid,  # replaced by the decision that adopts it (slice B)
        direction=direction,
        action=IntentAction.OPEN,
        horizon=s.horizon,
        expected_value_usd=None,
        cost_estimate_usd=None,
        requested_lot=ctx.initial_lot,
        valid_until=ctx.snapshot.valid_until,
        rationale=f"{s.manifest.strategy_id} rule v{s.manifest.version} -> {direction}",
        constraints=[],
    )


def _manifest(sid: str, catalog_id: str, tfs: list[Timeframe]) -> StrategyManifest:
    return StrategyManifest(
        strategy_id=sid,
        version=VERSION,
        artifact_hash=ARTIFACT_HASH,
        family="S01",
        catalog_ids=[catalog_id],
        technique_refs=[],
        timeframes=tfs,
        warmup_bars=0,
        parameters={},
        risk_tags=[],
        supported_modes=[Mode.SHADOW, Mode.DEMO],
    )


_RULES = [
    RuleStrategy(_manifest("trend_following", "CAT-S01-01", [H1, M15]),
                 (DIR_H1, STRUCT_M15, ADX_H1), _trend, H1),
    RuleStrategy(_manifest("scalping", "CAT-S01-02", [M5, M15]), (DIR_M5, DIR_M15), _scalp, M5),
    RuleStrategy(_manifest("pullback", "CAT-S01-03", [H1, M5]), (DIR_H1, ADX_H1, RSI_M5),
                 _pullback, M15),
    RuleStrategy(_manifest("breakout", "CAT-S01-04", [M15]), (RANGE_M15, SUPPORT_M15, RESIST_M15),
                 _breakout, M15),
    RuleStrategy(_manifest("range", "CAT-S01-05", [M15]),
                 (RANGE_M15, ATR_M15, SUPPORT_M15, RESIST_M15), _range, M15),
]  # fmt: skip


@dataclass(frozen=True)
class Hybrid:
    manifest: StrategyManifest
    members: tuple[RuleStrategy, ...]
    horizon: Timeframe = M15

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(k for m in self.members for k in m.required))

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        votes = [p.direction for m in self.members if (p := m.propose(ctx)) is not None]
        for direction in (Direction.UP, Direction.DOWN):
            if votes.count(direction) >= 2 and votes.count(direction) > len(votes) - votes.count(
                direction
            ):
                return _proposal(self, ctx, direction)
        return None


STRATEGIES: dict[str, RuleStrategy | Hybrid] = {s.manifest.strategy_id: s for s in _RULES}
STRATEGIES["hybrid"] = Hybrid(_manifest("hybrid", "CAT-S01-06", [H1, M15, M5]), tuple(_RULES))


def catalog_ids() -> set[str]:
    return {cid for s in STRATEGIES.values() for cid in s.manifest.catalog_ids}
