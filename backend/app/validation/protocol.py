"""The claim protocol record (#132; PRD #120; training-loop.md §6).

A protocol version is frozen before the first run of a claim. Its identity is the full
sha256 of its canonical JSON (sorted keys, no whitespace, UTF-8), so two documents with
the same content have the same hash however they are formatted, and any change makes a
new hash. A run counts only under a registered hash (`runlog.RunLog`).

Certified values are literal text with a rationale, never floats, so the hash cannot
drift with float formatting. The schema is generic on purpose: the values themselves
(targets, config space, regime rule, fold plan, gates) belong to the protocol document
the owner certifies, not to this code.
"""

import hashlib
import json
from itertools import combinations
from typing import Self

from pydantic import Field, StrictStr, model_validator

from app.core.contracts import ContractModel
from app.core.types import UtcDatetime


class ProtocolHashMismatch(ValueError):
    """The content does not hash to the expected protocol hash."""


class ProtocolWindow(ContractModel):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("a window must end after it starts")
        return self

    def overlaps(self, other: "ProtocolWindow") -> bool:
        return self.start < other.end and other.start < self.end


class ProtocolValue(ContractModel):
    name: StrictStr = Field(min_length=1)
    value: StrictStr = Field(min_length=1)
    """Literal text, e.g. "15 equal-mass bins, top-label"; never a float."""
    rationale: StrictStr = Field(min_length=1)


class Protocol(ContractModel):
    version: StrictStr = Field(min_length=1)
    certified_by: StrictStr = Field(min_length=1)
    certified_at: UtcDatetime
    data_source: StrictStr = Field(min_length=1)
    budget: int = Field(ge=1)
    """Distinct configs that may be counted under this version."""
    debug_window: ProtocolWindow
    """Free to read for debugging; never evidence."""
    unread_slice: ProtocolWindow | None = None
    """Read by nobody while debugging (PRD #120 story 42)."""
    holdout: ProtocolWindow
    """Opened once, by `RunLog.open_holdout`."""
    values: list[ProtocolValue] = Field(min_length=1)
    """Targets, config space, regime rule, fold plan, estimators: each with a rationale."""
    gates: list[ProtocolValue] = Field(min_length=1)
    """The binding pass rules, each with a rationale."""

    @model_validator(mode="after")
    def _sound(self) -> Self:
        names = [v.name for v in (*self.values, *self.gates)]
        if len(set(names)) != len(names):
            raise ValueError("value and gate names must be unique")
        windows = [w for w in (self.debug_window, self.unread_slice, self.holdout) if w]
        if any(a.overlaps(b) for a, b in combinations(windows, 2)):
            raise ValueError("the debug window, unread slice and holdout must not overlap")
        return self


def canonical_json(protocol: Protocol) -> bytes:
    return json.dumps(
        protocol.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def protocol_hash(protocol: Protocol) -> str:
    """64 hex chars: the full sha256 of the canonical JSON."""
    return hashlib.sha256(canonical_json(protocol)).hexdigest()


def load_protocol(text: str | bytes, expected_hash: str) -> Protocol:
    """Parse a protocol document and refuse it unless its content hashes to `expected_hash`."""
    protocol = Protocol.model_validate_json(text)
    actual = protocol_hash(protocol)
    if actual != expected_hash:
        raise ProtocolHashMismatch(f"protocol hashes to {actual}, expected {expected_hash}")
    return protocol
