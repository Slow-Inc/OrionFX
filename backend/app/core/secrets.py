"""Secret references and redaction (PRD-08 FR-004, FR-010, master plan §8.5).

A secret is never stored; config holds a reference `env:NAME` that is resolved
from the process environment at the moment of use.
"""

import os
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import BaseModel, Field

SECRET_REF_PATTERN = r"^env:[A-Z][A-Z0-9_]*$"
SecretRef = Annotated[str, Field(pattern=SECRET_REF_PATTERN)]

SECRET_KEY = re.compile(
    r"(^|[_-])(api_?keys?|secrets?|tokens?|passwords?|passwd|authorization|cookies?|credentials?)"
    r"($|[_-])",
    re.IGNORECASE,
)
MASK = "***"


def resolve_secret(ref: str, env: Mapping[str, str] | None = None) -> str | None:
    if not re.match(SECRET_REF_PATTERN, ref):
        raise ValueError("secret reference must look like env:NAME")
    source = os.environ if env is None else env
    return source.get(ref.removeprefix("env:"))


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Mask secret-looking keys and every occurrence of a known secret value."""
    known = [s for s in secrets if s]

    def walk(v: Any, key: str | None = None) -> Any:
        if key is not None and SECRET_KEY.search(key):
            return None if v is None else MASK  # whatever its shape; "unset" stays visible
        if isinstance(v, BaseModel):
            v = v.model_dump(mode="json")
        if isinstance(v, dict):
            return {k: walk(item, str(k)) for k, item in v.items()}
        if isinstance(v, list | tuple | set | frozenset):
            return [walk(item) for item in v]
        if isinstance(v, str):
            for s in known:
                v = v.replace(s, MASK)
            return v
        if isinstance(v, int | float | bool) or v is None:
            return v
        return walk(str(v))  # unknown objects are logged as text, still scrubbed

    return walk(value)
