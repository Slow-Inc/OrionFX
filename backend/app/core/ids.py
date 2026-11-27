"""Opaque, immutable identifiers (master plan §8.1)."""

import hashlib
import json
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def content_hash(*parts: object) -> str:
    """32 hex chars of the sha256 of the parts as canonical JSON: never depends on Python
    reprs or dict order."""
    blob = json.dumps(parts, default=str, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def stable_id(prefix: str, *parts: object) -> str:
    """Deterministic id: the same parts always give the same id (idempotent republish)."""
    return f"{prefix}-{content_hash(*parts)}"
