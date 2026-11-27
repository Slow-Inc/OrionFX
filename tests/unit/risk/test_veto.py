"""M06 slice B: pre-trade checks + Risk Veto (PRD-06 #14; RSK-04, RSK-06, RSK-08; AT-08, AT-13,
AT-32, AT-42; CAT-X02-*)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.config import UserConfig
from app.core.contracts import TradeIntent
from app.core.enums import ControlState, Mode, Side, Verdict
from app.core.states import ControlStatus
from app.execution.broker import BOT_MAGIC, AccountInfo, MarginMode, Position, Quote, SymbolSpec
from app.risk.policy import DEFAULT_POLICY
from app.risk.veto import ConfigEligibility, RiskContext, evaluate

D = Decimal
NOW = datetime(2026, 1, 5, 10, tzinfo=UTC)
CONFIG = UserConfig.model_validate(
    {
        "trading": {
            "initial_lot": "0.01",
            "profit_target_usd": "10",
            "max_drawdown_percent": "20",
            "strategies_enabled": ["trend_following", "pullback", "martingale"],
        }
    }
)
SPEC = SymbolSpec(
    symbol="XAUUSD",
    contract_size=D(100),
    tick_size=D("0.01"),
    volume_min=D("0.01"),
    volume_max=D(100),
    volume_step=D("0.01"),
    margin_per_lot=D(400),
    trade_allowed=True,
)
ACCOUNT = AccountInfo(
    login="demo-1",
    server="FakeBroker-Demo",
    currency="USD",
    balance=D(1000),
    equity=D(1000),
    margin=D(0),
    free_margin=D(1000),
    margin_mode=MarginMode.HEDGING,
    connected=True,
)


def _pos(volume: str, profit: str = "0", side: Side = Side.BUY) -> Position:
    return Position(
        position_id=f"pos-{volume}",
        symbol="XAUUSD",
        side=side,
        volume=D(volume),
        open_price=D(2000),
        magic=BOT_MAGIC,
        profit=D(profit),
        commission=D(0),
        swap=D(0),
    )


def _ctx(**over: object) -> RiskContext:
    base = RiskContext(
        now=NOW,
        mode=Mode.DEMO,
        live_allowed=False,
        control=ControlStatus(ControlState.RUNNING),
        account=ACCOUNT,
        spec=SPEC,
        quote=Quote("XAUUSD", D("2000.00"), D("2000.20"), NOW),
        bot_positions=[],
        ownership_ambiguous=False,
        execution_unresolved=False,
        config=CONFIG,
        policy=DEFAULT_POLICY,
        cycle_net_usd=None,
        eligibility=ConfigEligibility(CONFIG, DEFAULT_POLICY),
    )
    return replace(base, **over)  # type: ignore[arg-type]


def _intent(**over: object) -> TradeIntent:
    data: dict[str, object] = {
        "intent_id": "intent-1",
        "decision_id": "decision-1",
        "action": "OPEN",
        "side": "BUY",
        "symbol": "XAUUSD",
        "requested_lot": "0.01",
        "strategy_id": "trend_following",
        "cycle_id": "cyc-1",
        "mode": "DEMO",
        "expected_value_usd": "0.20",
        "prediction_ids": [],
        "valid_until": NOW + timedelta(seconds=30),
    }
    data.update(over)
    return TradeIntent.model_validate(data)


def _deny(intent: TradeIntent, ctx: RiskContext) -> list[str]:
    d = evaluate(intent, ctx)
    assert d.verdict is Verdict.DENY, d
    assert d.approved_lot is None
    return d.reason_codes


def test_clean_open_is_allowed_with_short_expiry() -> None:
    d = evaluate(_intent(), _ctx())
    assert d.verdict is Verdict.ALLOW and d.approved_lot == D("0.01")
    assert d.valid_until == NOW + timedelta(seconds=DEFAULT_POLICY.approval_ttl_seconds)
    assert d.policy_version == DEFAULT_POLICY.policy_version


@pytest.mark.parametrize(
    ("intent_over", "ctx_over", "code"),
    [
        ({"valid_until": NOW - timedelta(seconds=1)}, {}, "INTENT_EXPIRED"),
        ({"mode": "SHADOW"}, {}, "MODE_MISMATCH"),
        ({"mode": "LIVE"}, {"mode": Mode.LIVE}, "LIVE_NOT_ALLOWED"),
        ({}, {"control": ControlStatus(ControlState.PAUSED)}, "CONTROL_STATE_NO_ENTRY"),
        ({}, {"control": ControlStatus(ControlState.RUNNING, risk_locked=True)}, "RISK_LOCKED"),
        ({}, {"execution_unresolved": True}, "EXECUTION_UNRESOLVED"),
        ({}, {"account": replace(ACCOUNT, connected=False)}, "ACCOUNT_DISCONNECTED"),
        ({"symbol": "EURUSD"}, {}, "SYMBOL_NOT_CONFIGURED"),
        ({}, {"spec": None}, "SPEC_MISSING"),
        ({}, {"spec": replace(SPEC, trade_allowed=False)}, "MARKET_CLOSED"),
        ({}, {"quote": None}, "QUOTE_MISSING"),
        (
            {},
            {"quote": Quote("XAUUSD", D(2000), D("2000.2"), NOW - timedelta(seconds=30))},
            "QUOTE_STALE",
        ),
        ({}, {"quote": Quote("XAUUSD", D(2000), D("2002"), NOW)}, "SPREAD_TOO_WIDE"),
        ({}, {"ownership_ambiguous": True}, "OWNERSHIP_AMBIGUOUS"),
        ({"strategy_id": "breakout"}, {}, "STRATEGY_NOT_ELIGIBLE"),
    ],
)
def test_each_failed_check_denies_with_its_reason(
    intent_over: dict[str, object], ctx_over: dict[str, object], code: str
) -> None:
    assert code in _deny(_intent(**intent_over), _ctx(**ctx_over))


def test_config_incomplete_denies() -> None:
    assert "CONFIG_INCOMPLETE" in _deny(_intent(), _ctx(config=UserConfig()))


# --- AT-08: unsafe lots, margin, specs -----------------------------------------------


def test_initial_lot_below_broker_minimum_is_refused_not_rounded() -> None:
    spec = replace(SPEC, volume_min=D("0.10"))
    assert "LOT_BELOW_MIN" in _deny(_intent(), _ctx(spec=spec))


def test_lot_off_step_and_above_max() -> None:
    cfg = CONFIG.model_copy(
        update={"trading": CONFIG.trading.model_copy(update={"initial_lot": D("0.015")})}
    )
    codes = _deny(_intent(requested_lot="0.015"), _ctx(config=cfg))
    assert "LOT_NOT_ON_STEP" in codes
    assert "LOT_ABOVE_MAX" in _deny(
        _intent(requested_lot="0.01"), _ctx(spec=replace(SPEC, volume_max=D("0.005")))
    )


def test_first_entry_must_use_owner_initial_lot() -> None:
    assert "INITIAL_LOT_MISMATCH" in _deny(_intent(requested_lot="0.02"), _ctx())


def test_insufficient_margin_denies() -> None:
    poor = replace(ACCOUNT, equity=D(10), free_margin=D(5))
    assert "MARGIN_INSUFFICIENT" in _deny(_intent(), _ctx(account=poor))


def test_profile_exposure_and_position_caps() -> None:
    held = [
        _pos("0.01")
        for _ in range(DEFAULT_POLICY.caps_for(CONFIG.trading.risk_profile).max_positions)
    ]
    codes = _deny(_intent(action="ADD"), _ctx(bot_positions=held))
    assert "POSITION_CAP" in codes  # CAT-X02-01: no unlimited grid
    big = [_pos("0.05")]
    assert "EXPOSURE_CAP" in _deny(
        _intent(action="ADD", requested_lot="0.01"), _ctx(bot_positions=big)
    )


# --- AT-13, AT-32, AT-42: strategy eligibility and behaviour --------------------------------


def test_disabled_strategy_is_denied() -> None:
    assert "STRATEGY_NOT_ELIGIBLE" in _deny(_intent(strategy_id="range"), _ctx())


def test_martingale_by_name_denied_in_live() -> None:
    live = _ctx(mode=Mode.LIVE, live_allowed=True)
    assert "STRATEGY_NOT_ELIGIBLE" in _deny(_intent(mode="LIVE", strategy_id="martingale"), live)


def test_loss_chasing_size_up_denied_in_live_even_when_renamed() -> None:
    """AT-42 / CAT-X02-04: behaviour, not the name, decides."""
    live = _ctx(
        mode=Mode.LIVE,
        live_allowed=True,
        bot_positions=[_pos("0.01", profit="-5")],
        cycle_net_usd=D("-5"),
    )
    codes = _deny(
        _intent(mode="LIVE", action="ADD", requested_lot="0.02", strategy_id="pullback"), live
    )
    assert "MARTINGALE_BEHAVIOR" in codes


def test_loss_chasing_size_up_allowed_in_demo_within_caps() -> None:
    demo = _ctx(bot_positions=[_pos("0.01", profit="-5")], cycle_net_usd=D("-5"))
    d = evaluate(_intent(action="ADD", requested_lot="0.02", strategy_id="martingale"), demo)
    assert d.verdict is Verdict.ALLOW


def test_all_reasons_reported_not_just_the_first() -> None:
    locked = ControlStatus(ControlState.RUNNING, risk_locked=True)
    codes = _deny(_intent(), _ctx(control=locked, quote=None, ownership_ambiguous=True))
    assert {"RISK_LOCKED", "QUOTE_MISSING", "OWNERSHIP_AMBIGUOUS"} <= set(codes)


# --- exits are never blocked by entry checks ----------------------------------------------------


def test_close_all_allowed_while_locked_paused_and_stale() -> None:
    ctx = _ctx(
        control=ControlStatus(ControlState.PAUSED, risk_locked=True),
        quote=None,
        bot_positions=[_pos("0.01"), _pos("0.02")],
    )
    d = evaluate(_intent(action="CLOSE_ALL", side=None, requested_lot=None), ctx)
    assert d.verdict is Verdict.ALLOW and d.approved_lot == D("0.03")


def test_close_with_nothing_to_close_is_denied() -> None:
    codes = _deny(_intent(action="CLOSE_ALL", side=None, requested_lot=None), _ctx())
    assert codes == ["NOTHING_TO_CLOSE"]


def test_loss_chasing_via_open_action_is_also_caught() -> None:
    live = _ctx(
        mode=Mode.LIVE,
        live_allowed=True,
        bot_positions=[_pos("0.01", profit="-5")],
        cycle_net_usd=D("-5"),
    )
    codes = _deny(
        _intent(mode="LIVE", action="OPEN", requested_lot="0.02", strategy_id="pullback"), live
    )
    assert "MARTINGALE_BEHAVIOR" in codes


def test_adding_with_unknown_cycle_net_fails_closed() -> None:
    ctx = _ctx(bot_positions=[_pos("0.01")], cycle_net_usd=None)
    assert "CYCLE_STATE_UNKNOWN" in _deny(_intent(action="ADD", requested_lot="0.01"), ctx)


def test_exits_survive_live_revocation_and_mode_mismatch() -> None:
    ctx = _ctx(mode=Mode.LIVE, live_allowed=False, bot_positions=[_pos("0.02")])
    d = evaluate(_intent(action="CLOSE_ALL", side=None, requested_lot=None, mode="DEMO"), ctx)
    assert d.verdict is Verdict.ALLOW and d.approved_lot == D("0.02")


def test_quote_for_another_symbol_or_from_the_future_is_not_fresh() -> None:
    assert "QUOTE_MISSING" in _deny(_intent(), _ctx(quote=Quote("EURUSD", D(1), D(1), NOW)))
    future = Quote("XAUUSD", D(2000), D("2000.2"), NOW + timedelta(minutes=5))
    assert "QUOTE_STALE" in _deny(_intent(), _ctx(quote=future))


def test_zero_lot_step_denies_instead_of_crashing() -> None:
    assert "SPEC_INVALID" in _deny(_intent(), _ctx(spec=replace(SPEC, volume_step=D(0))))


def test_partial_close_must_respect_lot_rules() -> None:
    ctx = _ctx(bot_positions=[_pos("0.05")])
    codes = _deny(_intent(action="CLOSE", side=None, requested_lot="0.045"), ctx)
    assert "LOT_NOT_ON_STEP" in codes and "REMAINDER_LOT_BELOW_MIN" in codes
    ok = evaluate(_intent(action="CLOSE", side=None, requested_lot="0.02"), ctx)
    assert ok.verdict is Verdict.ALLOW and ok.approved_lot == D("0.02")


def test_spec_for_another_symbol_is_missing() -> None:
    assert "SPEC_MISSING" in _deny(_intent(), _ctx(spec=replace(SPEC, symbol="EURUSD")))


@pytest.mark.parametrize(
    ("bid", "ask"),
    [("2000.20", "2000.00"), ("0", "2000.00"), ("-1", "2000.00")],
)
def test_a_crossed_or_non_positive_quote_denies_entries(bid: str, ask: str) -> None:
    """#121 (a) / MAJ-06.7: ask < bid passed the spread check (negative spread)."""
    quote = Quote("XAUUSD", D(bid), D(ask), NOW)
    assert "QUOTE_INVALID" in _deny(_intent(), _ctx(quote=quote))


def test_a_crossed_quote_never_blocks_an_exit() -> None:
    crossed = Quote("XAUUSD", D("2000.20"), D("2000.00"), NOW)
    ctx = _ctx(quote=crossed, bot_positions=[_pos("0.01")])
    d = evaluate(_intent(action="CLOSE_ALL", side=None, requested_lot=None), ctx)
    assert d.verdict is Verdict.ALLOW


def test_negative_equity_is_already_denied_by_the_margin_check() -> None:
    """#121 (e): refuted. free_margin <= equity < 0 is below any floor of equity x 50%."""
    broke = replace(ACCOUNT, balance=D(-100), equity=D(-100), free_margin=D(-100))
    assert "MARGIN_INSUFFICIENT" in _deny(_intent(), _ctx(account=broke))
