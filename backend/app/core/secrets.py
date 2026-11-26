"""Secret references and redaction (PRD-08 FR-004, FR-010, master plan §8.5).

A secret is never stored; config holds a reference `env:NAME` that is resolved
from the process environment at the moment of use.
"""

import os
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import Field

SecretRef = Annotated[str, Field(pattern=r"^env:[A-Z][A-Z0-9_]*$")]

SECRET_KEY = re.compile(r"key|secret|token|password|authorization|cookie", re.IGNORECASE)
MASK = "***"


def resolve_secret(ref: str, env: Mapping[str, str] | None = None) -> str | None:
    if not re.fullmatch(r"env:[A-Z][A-Z0-9_]*", ref):
        raise ValueError("secret reference must look like env:NAME")
    source = os.environ if env is None else env
    return source.get(ref.removeprefix("env:"))


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Mask secret-looking keys and every occurrence of a known secret value."""
    known = [s for s in secrets if s]

    def walk(v: Any, key: str | None = None) -> Any:
        if key is not None and SECRET_KEY.search(key) and not isinstance(v, dict | list):
            return MASK
        if isinstance(v, dict):
            return {k: walk(item, str(k)) for k, item in v.items()}
        if isinstance(v, list):
            return [walk(item) for item in v]
        if isinstance(v, str):
            for s in known:
                v = v.replace(s, MASK)
        return v

    return walk(value)
