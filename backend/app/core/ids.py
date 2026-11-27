"""Opaque, immutable identifiers (master plan §8.1)."""

import hashlib
import json
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def stable_id(prefix: str, *parts: object) -> str:
    """Deterministic id: the same parts always give the same id (idempotent republish).
    Parts are hashed as canonical JSON, so the id never depends on Python reprs."""
    blob = json.dumps(parts, default=str, separators=(",", ":"), sort_keys=True)
    return f"{prefix}-{hashlib.sha256(blob.encode()).hexdigest()[:32]}"
