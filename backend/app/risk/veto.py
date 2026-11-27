"""Pre-trade checks and the Risk Veto (PRD-06 #14; RSK-04, RSK-06, RSK-08; AT-08, AT-13,
AT-32, AT-42; CAT-X02-01..04).

A pure function from (intent, context) to a `risk.decision`. Every failed check
is reported (not just the first). Entries fail closed on any missing, stale,
mismatched or malformed input. Exits (CLOSE/CLOSE_ALL) are never blocked by
entry checks — not by locks, pauses, stale data, Live revocation, a mislabelled
mode or an old intent — so the bot can always reduce risk; a partial close is
still checked against the broker's lot rules. M06's engine adds the cycle checks the
veto cannot make (see `Engine._engine_reasons`).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.core.config import UserConfig, missing_for_start
from app.core.contracts import RiskDecision, TradeIntent
from app.core.enums import IntentAction, Mode, Verdict
from app.core.states import ControlStatus, allows_new_exposure
from app.core.time import fresh
from app.execution.broker import AccountInfo, Position, Quote, SymbolSpec
from app.risk.policy import RiskPolicy

EXITS = (IntentAction.CLOSE, IntentAction.CLOSE_ALL)


def quote_fresh(quote: Quote, now: datetime, policy: RiskPolicy) -> bool:
    return fresh(quote.time, now, timedelta(seconds=policy.max_quote_age_seconds))


class Eligibility(Protocol):
    def allowed(self, strategy_id: str, mode: Mode) -> bool: ...


@dataclass(frozen=True)
class ConfigEligibility:
    """MVP eligibility: the owner enabled it, and mode-restricted strategies stay in
    their modes. Registry-backed eligibility records (UST-08, #21) replace this."""

    config: UserConfig
    policy: RiskPolicy

    def allowed(self, strategy_id: str, mode: Mode) -> bool:
        if strategy_id not in self.config.trading.strategies_enabled:
            return False
        restricted = self.policy.mode_restricted_strategies.get(strategy_id)
        return restricted is None or mode in restricted


@dataclass(frozen=True)
class RiskContext:
    """`control.risk_locked` is the single lock flag the veto reads; the engine loads
    it from the drawdown tracker's persisted state."""

    now: datetime
    mode: Mode
    live_allowed: bool
    control: ControlStatus
    account: AccountInfo
    spec: SymbolSpec | None
    quote: Quote | None
    bot_positions: list[Position]
    ownership_ambiguous: bool
    execution_unresolved: bool
    config: UserConfig
    policy: RiskPolicy
    cycle_net_usd: Decimal | None
    eligibility: Eligibility


def _deny(intent: TradeIntent, ctx: RiskContext, reasons: list[str]) -> RiskDecision:
    return RiskDecision(
        intent_id=intent.intent_id,
        verdict=Verdict.DENY,
        policy_version=ctx.policy.policy_version,
        reason_codes=reasons,
        decided_at=ctx.now,
    )


def _allow(intent: TradeIntent, ctx: RiskContext, lot: Decimal) -> RiskDecision:
    return RiskDecision(
        intent_id=intent.intent_id,
        verdict=Verdict.ALLOW,
        approved_lot=lot,
        policy_version=ctx.policy.policy_version,
        reason_codes=[],
        decided_at=ctx.now,
        valid_until=ctx.now + timedelta(seconds=ctx.policy.approval_ttl_seconds),
    )


def _held(ctx: RiskContext, symbol: str) -> list[Position]:
    return [p for p in ctx.bot_positions if p.symbol == symbol]


def _spec_reasons(spec: SymbolSpec | None, symbol: str) -> list[str]:
    if spec is None or spec.symbol != symbol:
        return ["SPEC_MISSING"]
    if min(spec.volume_min, spec.volume_step, spec.margin_per_lot, spec.contract_size) <= 0:
        return ["SPEC_INVALID"]  # never do arithmetic on a malformed spec
    return []


def _lot_reasons(lot: Decimal, spec: SymbolSpec) -> list[str]:
    reasons = []
    if lot < spec.volume_min:
        reasons.append("LOT_BELOW_MIN")  # refused, never rounded up (AT-08)
    if lot > spec.volume_max:
        reasons.append("LOT_ABOVE_MAX")
    if (lot - spec.volume_min) % spec.volume_step != 0:
        reasons.append("LOT_NOT_ON_STEP")
    return reasons


def _evaluate_exit(intent: TradeIntent, ctx: RiskContext) -> RiskDecision:
    held = sum((p.volume for p in _held(ctx, intent.symbol)), Decimal(0))
    if held == 0:
        return _deny(intent, ctx, ["NOTHING_TO_CLOSE"])
    if intent.action is IntentAction.CLOSE_ALL or not intent.requested_lot:
        return _allow(intent, ctx, held)
    lot = min(intent.requested_lot, held)
    if lot < held:  # a partial close must be a lot the broker accepts, leaving one too
        reasons = _spec_reasons(ctx.spec, intent.symbol)
        if not reasons and ctx.spec is not None:
            reasons = _lot_reasons(lot, ctx.spec) + [
                f"REMAINDER_{r}" for r in _lot_reasons(held - lot, ctx.spec)
            ]
        if reasons:
            return _deny(intent, ctx, reasons)
    return _allow(intent, ctx, lot)


def evaluate(intent: TradeIntent, ctx: RiskContext) -> RiskDecision:
    if intent.action in EXITS:
        return _evaluate_exit(intent, ctx)

    reasons: list[str] = []
    if intent.mode is not ctx.mode:
        reasons.append("MODE_MISMATCH")
    if ctx.mode is Mode.LIVE and not ctx.live_allowed:
        reasons.append("LIVE_NOT_ALLOWED")
    if intent.valid_until <= ctx.now:
        reasons.append("INTENT_EXPIRED")
    if missing_for_start(ctx.config):
        reasons.append("CONFIG_INCOMPLETE")
    if ctx.control.risk_locked:
        reasons.append("RISK_LOCKED")
    if not allows_new_exposure(ctx.control):
        reasons.append("CONTROL_STATE_NO_ENTRY")
    if ctx.execution_unresolved:
        reasons.append("EXECUTION_UNRESOLVED")
    if not ctx.account.connected:
        reasons.append("ACCOUNT_DISCONNECTED")
    if intent.symbol != ctx.config.trading.symbol:
        reasons.append("SYMBOL_NOT_CONFIGURED")
    if ctx.ownership_ambiguous:
        reasons.append("OWNERSHIP_AMBIGUOUS")
    if not ctx.eligibility.allowed(intent.strategy_id, ctx.mode):
        reasons.append("STRATEGY_NOT_ELIGIBLE")

    # The contract guarantees OPEN/ADD carry side and lot.
    lot = intent.requested_lot or Decimal(0)

    spec_problems = _spec_reasons(ctx.spec, intent.symbol)
    reasons += spec_problems
    if not spec_problems and ctx.spec is not None:
        if not ctx.spec.trade_allowed:
            reasons.append("MARKET_CLOSED")
        reasons += _lot_reasons(lot, ctx.spec)
        required_margin = lot * ctx.spec.margin_per_lot
        floor = ctx.account.equity * ctx.policy.min_free_margin_percent / 100
        if ctx.account.free_margin - required_margin < floor:
            reasons.append("MARGIN_INSUFFICIENT")

    quote = ctx.quote
    if quote is None or quote.symbol != intent.symbol:
        reasons.append("QUOTE_MISSING")
    else:
        if not quote_fresh(quote, ctx.now, ctx.policy):
            reasons.append("QUOTE_STALE")  # too old, or from the future (clock skew)
        if quote.bid <= 0 or quote.ask < quote.bid:
            reasons.append("QUOTE_INVALID")  # crossed or non-positive: never price off it
        if quote.ask - quote.bid > ctx.policy.max_spread:
            reasons.append("SPREAD_TOO_WIDE")

    held = _held(ctx, intent.symbol)
    if intent.action is IntentAction.OPEN and not held:
        initial = ctx.config.trading.initial_lot
        if initial is not None and lot != initial:
            reasons.append("INITIAL_LOT_MISMATCH")  # TRD-02: the owner sets the first lot

    caps = ctx.policy.caps_for(ctx.config.trading.risk_profile)
    if sum((p.volume for p in held), Decimal(0)) + lot > caps.max_open_lots:
        reasons.append("EXPOSURE_CAP")
    if len(held) + 1 > caps.max_positions:
        reasons.append("POSITION_CAP")  # CAT-X02-01: no unlimited grid

    # Behaviour, not the strategy's name or the action label: a bigger lot added on
    # top of a losing cycle is Martingale-style loss chasing, Shadow/Demo only
    # (RSK-06, CAT-X02-02/04, AT-42). Unknown cycle P&L fails closed.
    if held:
        largest = max(p.volume for p in held)
        if ctx.cycle_net_usd is None:
            reasons.append("CYCLE_STATE_UNKNOWN")
        elif (
            ctx.cycle_net_usd < 0 and lot > largest and ctx.mode not in ctx.policy.martingale_modes
        ):
            reasons.append("MARTINGALE_BEHAVIOR")

    if reasons:
        return _deny(intent, ctx, reasons)
    return _allow(intent, ctx, lot)
