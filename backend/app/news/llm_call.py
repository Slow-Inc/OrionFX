"""One bounded, validated LLM call shared by every M03 stage (#39, #40).

The answer is parsed by the caller's `parse` (JSON numbers as Decimal, one code
fence allowed, deep nesting refused). A provider failure or an invalid answer is
retried once; after that the call returns no value and a detail that names the
problem but never the provider's raw error text, which may carry request data.
"""

import json
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.news.provider import Provider, ProviderError

TIMEOUT_S = 20.0  # [PROPOSED]
ATTEMPTS = 2  # one retry [PROPOSED]
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def load_json(text: str) -> Any:
    fenced = _FENCE.match(text)
    try:
        # Numbers are parsed as Decimal: model scores never pass through binary floats.
        return json.loads(fenced.group(1) if fenced else text, parse_float=Decimal)
    except RecursionError:
        raise ValueError("answer nested too deeply") from None


def describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors(include_input=False)[0]
        return f"{'.'.join(str(p) for p in err['loc']) or 'answer'}: {err['msg']}"
    return (str(exc).splitlines() or [type(exc).__name__])[0]


def ask[T](
    provider: Provider, prompt: str, parse: Callable[[str], T], *, timeout_s: float = TIMEOUT_S
) -> tuple[T | None, str]:
    """(parsed answer, "") on success; (None, why) after ATTEMPTS failures."""
    detail = ""
    for _ in range(ATTEMPTS):
        try:
            return parse(provider.complete(prompt, timeout_s=timeout_s)), ""
        except ProviderError as exc:
            detail = f"provider {type(exc).__name__}"  # never the raw error text
        except (ValueError, ValidationError) as exc:  # JSON errors are ValueErrors
            detail = f"invalid answer: {describe(exc)}"
    return None, f"failed after {ATTEMPTS} attempts: {detail}"
