"""Pre-trade checks and the Risk Veto (PRD-06 #14; RSK-04, RSK-06, RSK-08; AT-08, AT-13,
AT-32, AT-42; CAT-X02-01..04).

A pure function from (intent, context) to a `risk.decision`. Every failed check
is reported (not just the first). Entries fail closed on any missing or stale
input. Exits (CLOSE/CLOSE_ALL) are never blocked by entry checks, so a locked,
paused or blind bot can always reduce risk.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.core.config import UserConfig, missing_for_start
from app.core.contracts import RiskDecision, TradeIntent
from app.core.enums import IntentAction, Mode, Verdict
from app.core.states import ControlStatus, allows_new_exposure
from app.execution.broker import AccountInfo, Position, Quote, SymbolSpec
from app.risk.policy import RiskPolicy

EXITS = (IntentAction.CLOSE, IntentAction.CLOSE_ALL)
MARTINGALE_IDS = frozenset({"martingale"})


class Eligibility(Protocol):
    def allowed(self, strategy_id: str, mode: Mode) -> bool: ...


@dataclass(frozen=True)
class ConfigEligibility:
    """MVP eligibility: the owner enabled it, and Martingale stays in its modes.
    Registry-backed eligibility records (UST-08, #21) replace this when they exist."""

    config: UserConfig
    policy: RiskPolicy

    def allowed(self, strategy_id: str, mode: Mode) -> bool:
        if strategy_id not in self.config.trading.strategies_enabled:
            return False
        return strategy_id not in MARTINGALE_IDS or mode in self.policy.martingale_modes


@dataclass(frozen=True)
class RiskContext:
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
    risk_locked: bool
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


def _mode_reasons(intent: TradeIntent, ctx: RiskContext) -> list[str]:
    reasons = []
    if intent.mode is not ctx.mode:
        reasons.append("MODE_MISMATCH")
    if ctx.mode is Mode.LIVE and not ctx.live_allowed:
        reasons.append("LIVE_NOT_ALLOWED")
    return reasons


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
    reasons = _mode_reasons(intent, ctx)
    held = sum((p.volume for p in ctx.bot_positions if p.symbol == intent.symbol), Decimal(0))
    if held == 0:
        reasons.append("NOTHING_TO_CLOSE")
    if reasons:
        return _deny(intent, ctx, reasons)
    lot = (
        held if intent.action is IntentAction.CLOSE_ALL else min(intent.requested_lot or held, held)
    )
    return _allow(intent, ctx, lot)


def evaluate(intent: TradeIntent, ctx: RiskContext) -> RiskDecision:
    if intent.action in EXITS:
        return _evaluate_exit(intent, ctx)

    reasons = _mode_reasons(intent, ctx)
    if intent.valid_until <= ctx.now:
        reasons.append("INTENT_EXPIRED")
    if missing_for_start(ctx.config):
        reasons.append("CONFIG_INCOMPLETE")
    if not allows_new_exposure(ctx.control):
        reasons.append("CONTROL_STATE_NO_ENTRY")
    if ctx.risk_locked:
        reasons.append("RISK_LOCKED")
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

    if ctx.spec is None:
        reasons.append("SPEC_MISSING")
    else:
        if not ctx.spec.trade_allowed:
            reasons.append("MARKET_CLOSED")
        reasons += _lot_reasons(lot, ctx.spec)
        required_margin = lot * ctx.spec.margin_per_lot
        floor = ctx.account.equity * ctx.policy.min_free_margin_percent / 100
        if ctx.account.free_margin - required_margin < floor:
            reasons.append("MARGIN_INSUFFICIENT")

    if ctx.quote is None:
        reasons.append("QUOTE_MISSING")
    else:
        if ctx.now - ctx.quote.time > timedelta(seconds=ctx.policy.max_quote_age_seconds):
            reasons.append("QUOTE_STALE")
        if ctx.quote.ask - ctx.quote.bid > ctx.policy.max_spread:
            reasons.append("SPREAD_TOO_WIDE")

    held = [p for p in ctx.bot_positions if p.symbol == intent.symbol]
    if intent.action is IntentAction.OPEN and not held:
        initial = ctx.config.trading.initial_lot
        if initial is not None and lot != initial:
            reasons.append("INITIAL_LOT_MISMATCH")  # TRD-02: the owner sets the first lot

    caps = ctx.policy.caps_for(ctx.config.trading.risk_profile)
    if sum((p.volume for p in held), Decimal(0)) + lot > caps.max_open_lots:
        reasons.append("EXPOSURE_CAP")
    if len(held) + 1 > caps.max_positions:
        reasons.append("POSITION_CAP")  # CAT-X02-01: no unlimited grid

    # Behaviour, not the strategy's name: a bigger lot added while the cycle loses is
    # Martingale-style loss chasing, Shadow/Demo only (RSK-06, CAT-X02-02/04, AT-42).
    losing = ctx.cycle_net_usd is not None and ctx.cycle_net_usd < 0
    largest = max((p.volume for p in held), default=Decimal(0))
    if (
        intent.action is IntentAction.ADD
        and losing
        and lot > largest
        and ctx.mode not in ctx.policy.martingale_modes
    ):
        reasons.append("MARTINGALE_BEHAVIOR")

    if reasons:
        return _deny(intent, ctx, reasons)
    return _allow(intent, ctx, lot)
