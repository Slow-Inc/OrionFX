"""What every M03 LLM stage shares (#39, #40): one bounded, validated call, the
component's health report, and the no-provider message.

The answer is parsed by the caller's `parse` (JSON numbers as Decimal, one code
fence allowed, deep nesting refused). Any failure, from the provider or from
the answer, is retried once; after that the call returns no value and a detail
that names the kind of problem but never the provider's raw error text (it may
carry request data) nor long model output. Catching every exception from the
adapter is deliberate: a real transport error must degrade to WAIT, never crash
the cycle.
"""

import json
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.core.contracts import HealthUpdated
from app.core.enums import HealthStatus
from app.news.provider import Provider

__all__ = ["COMPONENT", "NO_PROVIDER", "TIMEOUT_S", "ask", "health", "load_json"]

COMPONENT = "M03"
NO_PROVIDER = "no LLM provider configured"
TIMEOUT_S = 20.0  # [PROPOSED]
ATTEMPTS = 2  # one retry [PROPOSED]
DETAIL_MAX = 160
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def health(status: HealthStatus, detail: str | None, now: datetime) -> HealthUpdated:
    return HealthUpdated(component=COMPONENT, status=status, last_heartbeat=now, detail=detail)


def load_json(text: str) -> Any:
    fenced = _FENCE.match(text)
    try:
        # Numbers are parsed as Decimal: model scores never pass through binary floats.
        return json.loads(fenced.group(1) if fenced else text, parse_float=Decimal)
    except RecursionError:
        raise ValueError("answer nested too deeply") from None


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        err = exc.errors(include_input=False)[0]
        text = f"{'.'.join(str(p) for p in err['loc']) or 'answer'}: {err['msg']}"
    else:
        text = (str(exc).splitlines() or [type(exc).__name__])[0]
    return text[:DETAIL_MAX]


def ask[T](
    provider: Provider, prompt: str, parse: Callable[[str], T], *, timeout_s: float = TIMEOUT_S
) -> tuple[T | None, str]:
    """(parsed answer, "") on success; (None, why) after ATTEMPTS failures."""
    detail = ""
    for _ in range(ATTEMPTS):
        try:
            text = provider.complete(prompt, timeout_s=timeout_s)
        except Exception as exc:  # any adapter failure degrades; never echo its text
            detail = f"provider {type(exc).__name__}"
            continue
        try:
            return parse(text), ""
        except (ValueError, ValidationError) as exc:  # JSON errors are ValueErrors
            detail = f"invalid answer: {_describe(exc)}"
    return None, f"failed after {ATTEMPTS} attempts: {detail}"
