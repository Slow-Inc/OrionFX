"""Opaque, immutable identifiers (master plan §8.1)."""

from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"
