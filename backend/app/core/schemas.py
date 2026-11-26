"""JSON Schema export of the contracts for non-Python consumers (dashboard, EA bridge)."""

import json
from typing import Any

from app.core.contracts import EVENT_PAYLOADS
from app.core.events import EVENT_ADAPTER


def _text(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def generate_schemas() -> dict[str, str]:
    """Map `<name>.schema.json` -> pretty JSON text (LF, trailing newline)."""
    out = {"envelope.schema.json": _text(EVENT_ADAPTER.json_schema(mode="serialization"))}
    for event_type, model in EVENT_PAYLOADS.items():
        out[f"{event_type}.schema.json"] = _text(model.model_json_schema(mode="serialization"))
    return out
