"""The hashed claim-protocol record (#132; training-loop.md §6). Every value in these
fixtures is synthetic: the certified values live in the protocol document, not here."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.validation.protocol import (
    Protocol,
    ProtocolHashMismatch,
    ProtocolValue,
    ProtocolWindow,
    canonical_json,
    load_protocol,
    protocol_hash,
)


def _t(year: int, month: int = 1) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def make_protocol(**overrides: object) -> Protocol:
    fields: dict[str, object] = {
        "version": "test-protocol-1",
        "certified_by": "test",
        "certified_at": _t(2030),
        "data_source": "synthetic-source",
        "budget": 3,
        "debug_window": ProtocolWindow(start=_t(2000), end=_t(2001)),
        "unread_slice": ProtocolWindow(start=_t(2004), end=_t(2004, 6)),
        "holdout": ProtocolWindow(start=_t(2005), end=_t(2006)),
        "values": [ProtocolValue(name="fold_plan", value="synthetic folds", rationale="fixture")],
        "gates": [ProtocolValue(name="G-test", value="metric > 0", rationale="fixture")],
    }
    return Protocol.model_validate(fields | overrides)


@pytest.mark.parametrize("field", ["values", "gates"])
def test_a_protocol_needs_at_least_one_value_and_one_gate(field: str) -> None:
    with pytest.raises(ValidationError):
        make_protocol(**{field: []})


def test_the_hash_is_a_full_sha256_of_canonical_json() -> None:
    p = make_protocol()
    h = protocol_hash(p)
    assert len(h) == 64
    assert int(h, 16) >= 0
    assert canonical_json(p) == canonical_json(Protocol.model_validate_json(canonical_json(p)))


def test_any_change_changes_the_hash() -> None:
    assert protocol_hash(make_protocol()) != protocol_hash(make_protocol(budget=4))


def test_load_verifies_the_hash() -> None:
    p = make_protocol()
    text = canonical_json(p).decode()
    assert load_protocol(text, protocol_hash(p)) == p
    with pytest.raises(ProtocolHashMismatch):
        load_protocol(text, protocol_hash(make_protocol(budget=4)))


def test_load_accepts_any_formatting_of_the_same_content() -> None:
    p = make_protocol()
    assert load_protocol(p.model_dump_json(indent=2), protocol_hash(p)) == p


def test_values_are_literal_text_never_floats() -> None:
    with pytest.raises(ValidationError):
        ProtocolValue.model_validate({"name": "x", "value": 0.02, "rationale": "r"})


def test_names_are_unique() -> None:
    v = ProtocolValue(name="dup", value="1", rationale="r")
    with pytest.raises(ValidationError):
        make_protocol(values=[v, v])


def test_the_debug_window_cannot_overlap_the_holdout_or_unread_slice() -> None:
    with pytest.raises(ValidationError):
        make_protocol(debug_window=ProtocolWindow(start=_t(2005, 6), end=_t(2007)))
    with pytest.raises(ValidationError):
        make_protocol(debug_window=ProtocolWindow(start=_t(2004, 3), end=_t(2004, 4)))


def test_a_window_must_end_after_it_starts() -> None:
    with pytest.raises(ValidationError):
        ProtocolWindow(start=_t(2001), end=_t(2000))


def test_the_budget_is_positive() -> None:
    with pytest.raises(ValidationError):
        make_protocol(budget=0)
