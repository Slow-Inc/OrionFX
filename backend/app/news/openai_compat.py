"""OpenAI-compatible LLM provider for M03 (#100, PRD #98; LLM-01, LLM-05).

Implements the `Provider` port over chat completions (e.g. OpenCode Go). One user
message, no tools or functions: the port has nowhere to put them, so the model can never
reach the broker. The key is a `SecretRef`, resolved at call time only, never held in
config, logged or echoed. Every failure becomes `ProviderError` / `ProviderTimeout` whose
text names the kind of failure only (status code, exception type), never the response
body, which can carry account ids. A rate limit (429) is waited out once, as `Retry-After`
says, when the wait fits in the caller's timeout; otherwise `ProviderRateLimited`, which
M03 does not retry at once. `timeout_s` bounds the whole call, waits and a slowly
trickling body included (httpx's own timeouts are per read). The allowance a call drew
(OpenCode's `usage.opencode.allowance`, in plan dollars) is kept for cost tracking. Build
one adapter per worker and close it: it holds a connection pool and the session id that
lets the provider cache the prompt prefix.
"""

import json
import time
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import LlmConfig
from app.core.secrets import resolve_secret
from app.news.provider import ProviderError, ProviderRateLimited, ProviderTimeout

__all__ = ["OpenAICompatibleProvider", "provider_from_config"]


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_ref: str,
        reasoning_effort: str | None = None,
        provider_id: str = "openai-compatible",
        env: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider_id = provider_id
        self.model = model
        self.last_allowance: Decimal | None = None
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key_ref = api_key_ref
        self._effort = reasoning_effort
        self._env = env
        self._session = f"orionfx-m03-{uuid.uuid4().hex[:12]}"  # stable: enables prompt caching
        self._client = httpx.Client(transport=transport)
        self._sleep = sleep
        self._clock = clock

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenAICompatibleProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider(provider_id={self.provider_id!r}, model={self.model!r})"

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        self.last_allowance = None
        key = resolve_secret(self._key_ref, self._env)
        if not key:
            raise ProviderError("API key not set")
        body: dict[str, Any] = {"model": self.model,
                                "messages": [{"role": "user", "content": prompt}]}  # fmt: skip
        if self._effort:
            body["reasoning_effort"] = self._effort
        headers = {"Authorization": f"Bearer {key}", "x-opencode-session": self._session}
        deadline = self._clock() + timeout_s
        for attempt in range(2):
            status, raw, wait = self._post(body, headers, deadline)
            if status == 429:
                if attempt == 0 and wait is not None and wait < deadline - self._clock():
                    self._sleep(wait)
                    continue
                break
            if status != 200:
                raise ProviderError(f"HTTP {status}")
            return self._content(raw)
        raise ProviderRateLimited("rate limited")

    def _post(
        self, body: dict[str, Any], headers: dict[str, str], deadline: float
    ) -> tuple[int, bytes, float | None]:
        """(status, body, Retry-After seconds), reading the body only until `deadline`."""
        left = deadline - self._clock()
        if left <= 0:
            raise ProviderTimeout("timed out")
        try:
            with self._client.stream("POST", self._url, json=body, headers=headers,
                                     timeout=left) as r:  # fmt: skip
                if r.status_code != 200:
                    return r.status_code, b"", _retry_after(r)
                chunks = []
                for chunk in r.iter_bytes():
                    if self._clock() > deadline:
                        raise ProviderTimeout("timed out")
                    chunks.append(chunk)
                return 200, b"".join(chunks), None
        except httpx.TimeoutException:
            raise ProviderTimeout("timed out") from None
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport {type(exc).__name__}") from None

    def _content(self, raw: bytes) -> str:
        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("malformed response") from None
        if not isinstance(content, str) or not content:
            raise ProviderError("empty response")
        self.last_allowance = _allowance(data)
        return content


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return max(0.0, float(r.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


def _allowance(data: Any) -> Decimal | None:
    try:
        return Decimal(str(data["usage"]["opencode"]["allowance"]))
    except (KeyError, TypeError, InvalidOperation):
        return None


def provider_from_config(
    cfg: LlmConfig, *, env: Mapping[str, str] | None = None
) -> OpenAICompatibleProvider | None:
    """The configured provider, or None when news handling is off or the provider, model,
    base URL or key reference is missing; M03 then reports DOWN ("no LLM provider
    configured"), deliberately the same for off as for unset: no news analysis either way.
    Build it once per worker and close it when done."""
    if cfg.news_handling == "off" or not (cfg.provider and cfg.model and cfg.base_url):
        return None
    if cfg.api_key_ref is None:
        return None
    return OpenAICompatibleProvider(
        base_url=cfg.base_url, model=cfg.model, api_key_ref=cfg.api_key_ref,
        reasoning_effort=cfg.reasoning_effort, provider_id=cfg.provider, env=env,
    )  # fmt: skip
