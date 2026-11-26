"""Contract tests for the shared event envelope and catalog (PRD-X01 X01-FR-001..004, 008)."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.contracts import (
    EVENT_PAYLOADS,
    CandleClosed,
    CycleUpdated,
    MarketSnapshot,
    RiskDecision,
    TradeIntent,
)
from app.core.enums import Mode
from app.core.events import ENVELOPES, make_event, parse_event
from app.core.schemas import generate_schemas

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _intent(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent_id": "intent-1",
        "decision_id": "decision-1",
        "action": "OPEN",
        "side": "BUY",
        "symbol": "XAUUSD",
        "requested_lot": "0.01",
        "strategy_id": "trend_following",
        "cycle_id": "cycle-1",
        "mode": "DEMO",
        "expected_value_usd": "0.20",
        "prediction_ids": ["prediction-1"],
        "valid_until": "2026-01-01T00:01:00Z",
    }
    base.update(over)
    return base


def _envelope(**over: object) -> dict[str, object]:
    base = make_event(
        "trade.intent", source="M05", payload=_intent(), account_id="a", bot_id="b", mode=Mode.DEMO
    )
    return base.model_dump() | over


def _catalog_from_master_plan() -> set[str]:
    """Derive the event set from master plan §8.2 instead of listing it here."""
    text = (REPO / "docs/plans/master_planV1.1.md").read_text(encoding="utf-8")
    section = text.split("### 8.2 Event catalog and ownership", 1)[1].split("###", 1)[0]
    events = set(re.findall(r"^\| `([a-z_]+\.[a-z_]+)` \|", section, flags=re.MULTILINE))
    # V1.1 §15.5 adds the strategy/technique events on one "Proposed events:" line.
    assert "Proposed events:" in text, "§15.5 proposed-events line not found"
    proposed = text.split("Proposed events:", 1)[1].splitlines()[0]
    return events | set(re.findall(r"`([a-z_]+(?:\.[a-z_]+)+)` \(", proposed))


def test_catalog_covers_every_master_plan_event() -> None:
    planned = _catalog_from_master_plan()
    assert set(EVENT_PAYLOADS) == planned, f"parsed {len(planned)} events from §8.2"
    assert set(ENVELOPES) == planned


def test_envelope_accepts_matching_payload() -> None:
    env = make_event(
        "trade.intent",
        source="M05",
        payload=_intent(),
        account_id="acc",
        bot_id="b1",
        mode=Mode.DEMO,
    )
    assert isinstance(env.payload, TradeIntent)
    assert env.payload.requested_lot == Decimal("0.01")
    assert env.schema_version == "1.0"


def test_envelope_rejects_payload_of_wrong_type() -> None:
    wrong = RiskDecision(
        intent_id="intent-1",
        verdict="DENY",
        policy_version="p1",
        reason_codes=["STALE"],
        decided_at=NOW,
    )
    with pytest.raises(ValidationError):
        parse_event(_envelope(payload=wrong))


def test_envelope_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        make_event(
            "trade.yolo", source="M05", payload={}, account_id="a", bot_id="b", mode=Mode.DEMO
        )


def test_envelope_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        parse_event(_envelope(occurred_at=datetime(2026, 1, 1)))


def test_envelope_rejects_non_utc_offset() -> None:
    with pytest.raises(ValidationError):
        parse_event(_envelope(payload=_intent(valid_until="2026-01-01T07:00:00+07:00")))


def test_payload_mode_must_match_envelope_mode() -> None:
    with pytest.raises(ValidationError, match="mode"):
        parse_event(_envelope(payload=_intent(mode="LIVE")))


def test_payload_cycle_must_match_envelope_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle_id"):
        parse_event(_envelope(cycle_id="cycle-2"))


def test_money_rejects_float() -> None:
    with pytest.raises(ValidationError):
        TradeIntent.model_validate(_intent(requested_lot=0.01))


def test_lot_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        TradeIntent.model_validate(_intent(requested_lot="0"))


def test_unknown_is_none_not_zero() -> None:
    intent = TradeIntent.model_validate(_intent(expected_value_usd=None))
    assert intent.expected_value_usd is None


def test_envelope_round_trips_through_json() -> None:
    env = make_event(
        "trade.intent", source="M05", payload=_intent(), account_id="a", bot_id="b", mode=Mode.DEMO
    )
    raw = env.model_dump_json()
    assert '"requested_lot":"0.01"' in raw, "money must serialize as a string, not a float"
    assert parse_event(raw) == env


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TradeIntent.model_validate(_intent(surprise="x"))


def test_envelope_schema_is_discriminated_on_event_type() -> None:
    schema = generate_schemas()["envelope.schema.json"]
    assert '"discriminator"' in schema and '"propertyName": "event_type"' in schema


def test_schemas_up_to_date() -> None:
    contracts = REPO / "docs/contracts"
    on_disk = {p.name: p.read_text(encoding="utf-8") for p in contracts.glob("*.schema.json")}
    assert on_disk == generate_schemas(), "run: uv run python scripts/gen_schemas.py"


def test_open_intent_requires_side_and_lot() -> None:
    with pytest.raises(ValidationError, match="side and requested_lot"):
        TradeIntent.model_validate(_intent(side=None))
    with pytest.raises(ValidationError, match="side and requested_lot"):
        TradeIntent.model_validate(_intent(requested_lot=None))
    TradeIntent.model_validate(_intent(action="CLOSE_ALL", side=None, requested_lot=None))


def _risk(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent_id": "i",
        "verdict": "ALLOW",
        "approved_lot": "0.01",
        "policy_version": "p",
        "reason_codes": [],
        "decided_at": NOW,
        "valid_until": "2026-01-01T00:00:05Z",
    }
    return base | over


def test_allow_requires_bounded_lot_and_expiry() -> None:
    RiskDecision.model_validate(_risk())
    for missing in ("approved_lot", "valid_until"):
        with pytest.raises(ValidationError, match="ALLOW"):
            RiskDecision.model_validate(_risk(**{missing: None}))
    with pytest.raises(ValidationError, match="DENY"):
        RiskDecision.model_validate(_risk(verdict="DENY"))


def test_unknown_schema_version_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_event(_envelope(schema_version="9.9"))


def test_make_event_copies_payload_cycle_into_envelope() -> None:
    env = make_event(
        "trade.intent", source="M05", payload=_intent(), account_id="a", bot_id="b", mode=Mode.DEMO
    )
    assert env.cycle_id == "cycle-1"


def test_envelope_without_cycle_rejects_payload_with_cycle() -> None:
    with pytest.raises(ValidationError, match="cycle_id"):
        parse_event(_envelope(cycle_id=None))


def _candle(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "open_time": "2026-01-01T00:00:00Z",
        "close_time": "2026-01-01T00:05:00Z",
        "open": "2000",
        "high": "2010",
        "low": "1990",
        "close": "2005",
        "volume": "10",
        "available_at": "2026-01-01T00:05:00Z",
    }
    return base | over


def test_candle_rejects_impossible_bars_and_lookahead() -> None:
    CandleClosed.model_validate(_candle())
    for bad in (
        {"high": "1980"},
        {"low": "2006"},
        {"close_time": "2025-12-31T23:59:00Z"},
        {"available_at": "2026-01-01T00:04:59Z"},
    ):
        with pytest.raises(ValidationError):
            CandleClosed.model_validate(_candle(**bad))


def test_snapshot_rejects_ask_below_bid() -> None:
    with pytest.raises(ValidationError, match="ask"):
        MarketSnapshot(
            symbol="XAUUSD", bid="2001", ask="2000", spread="0", tick_time=NOW, quality="OK"
        )


def test_cycle_target_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        CycleUpdated(
            state="ACTIVE",
            target_usd="0",
            cycle_net_estimated_usd=None,
            peak_net_usd=None,
            trailing_floor_usd=None,
            trailing_active=False,
        )
