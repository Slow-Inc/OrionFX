"""The common event envelope (master plan §8.1).

One envelope model per event type is generated from `EVENT_PAYLOADS`, and the
union is discriminated on `event_type`, so pydantic itself rejects unknown event
types and mismatched payloads, and the exported JSON Schema says which payload
belongs to which event. Delivery is at-least-once: consumers deduplicate by
`event_id`.
"""

from typing import Annotated, Any, Literal, Self, Union, cast

from pydantic import Field, TypeAdapter, create_model, model_validator

from app.core.contracts import EVENT_PAYLOADS, ContractModel
from app.core.enums import Mode
from app.core.ids import new_id
from app.core.time import utc_now
from app.core.types import UtcDatetime

SCHEMA_VERSION: Literal["1.0"] = "1.0"


class EventEnvelope(ContractModel):
    """Fields shared by every event; concrete subclasses add `event_type` and `payload`."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_id: str
    event_type: str
    occurred_at: UtcDatetime
    recorded_at: UtcDatetime
    source: str
    correlation_id: str
    causation_id: str | None = None
    account_id: str
    bot_id: str
    cycle_id: str | None = None
    mode: Mode
    payload: ContractModel

    @model_validator(mode="after")
    def _payload_context_matches(self) -> Self:
        """A payload that carries its own mode/cycle must agree with the envelope."""
        payload_mode = getattr(self.payload, "mode", None)
        if payload_mode is not None and payload_mode != self.mode:
            raise ValueError(f"payload mode {payload_mode} != envelope mode {self.mode}")
        payload_cycle = getattr(self.payload, "cycle_id", None)
        if payload_cycle is not None and payload_cycle != self.cycle_id:
            raise ValueError("payload cycle_id differs from envelope cycle_id")
        return self


def _envelope_for(event_type: str, payload: type[ContractModel]) -> type[EventEnvelope]:
    # Types built at runtime from the catalog; mypy cannot see through them, hence Any.
    literal: Any = Literal[event_type]
    return create_model(
        f"{payload.__name__}Event",
        __base__=EventEnvelope,
        event_type=(literal, ...),
        payload=(payload, ...),
    )


ENVELOPES: dict[str, type[EventEnvelope]] = {
    event_type: _envelope_for(event_type, payload) for event_type, payload in EVENT_PAYLOADS.items()
}

_any_event: Any = Annotated[
    Union[tuple(ENVELOPES.values())],  # noqa: UP007 — built at runtime from the catalog
    Field(discriminator="event_type"),
]
EVENT_ADAPTER: TypeAdapter[EventEnvelope] = cast(
    TypeAdapter[EventEnvelope], TypeAdapter(_any_event)
)


def parse_event(data: dict[str, Any] | str | bytes) -> EventEnvelope:
    """Validate a dict or JSON text into the concrete envelope for its event_type."""
    if isinstance(data, dict):
        return EVENT_ADAPTER.validate_python(data)
    return EVENT_ADAPTER.validate_json(data)


def _payload_cycle(payload: ContractModel | dict[str, Any]) -> str | None:
    value = (
        payload.get("cycle_id") if isinstance(payload, dict) else getattr(payload, "cycle_id", None)
    )
    return value if isinstance(value, str) else None


def make_event(
    event_type: str,
    *,
    source: str,
    payload: ContractModel | dict[str, Any],
    account_id: str,
    bot_id: str,
    mode: Mode,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    cycle_id: str | None = None,
) -> EventEnvelope:
    now = utc_now()
    return parse_event(
        {
            "event_id": new_id("evt"),
            "event_type": event_type,
            "occurred_at": now,
            "recorded_at": now,
            "source": source,
            "correlation_id": correlation_id or new_id("corr"),
            "causation_id": causation_id,
            "account_id": account_id,
            "bot_id": bot_id,
            "cycle_id": cycle_id or _payload_cycle(payload),
            "mode": mode,
            "payload": payload,
        }
    )
