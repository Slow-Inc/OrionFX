"""M03 live provider: an OpenAI-compatible chat-completions adapter (#100, PRD #98; LLM-01,
LLM-05). Every test uses httpx.MockTransport; nothing touches the network."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import LlmConfig, UserConfig
from app.news.openai_compat import OpenAICompatibleProvider, provider_from_config
from app.news.provider import Provider, ProviderError, ProviderTimeout

BASE = "https://llm.example/inference/go/openai/v1"
KEY = "sk-test-secret-123"
ENV = {"LLM_KEY": KEY}


def _ok(content: str = '{"direction": "UP"}', allowance: float = 0.00012) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"opencode": {"allowance": allowance, "billing_source": "go"}},
        },
    )


def _provider(handler, *, effort: str | None = "max", sleeps: list[float] | None = None):  # type: ignore[no-untyped-def]
    slept = sleeps if sleeps is not None else []
    return OpenAICompatibleProvider(
        base_url=BASE,
        model="qwen3.8-flash",
        api_key_ref="env:LLM_KEY",
        reasoning_effort=effort,
        env=ENV,
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
    )


def test_it_sends_one_chat_completion_with_the_key_session_and_effort() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _ok("answer text")

    p = _provider(handler)
    port: Provider = p  # mypy checks it implements the M03 port
    assert port.complete("the prompt", timeout_s=30) == "answer text"
    (req,) = seen
    assert str(req.url) == f"{BASE}/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["x-opencode-session"]
    body = json.loads(req.content)
    assert body["model"] == "qwen3.8-flash"
    assert body["messages"] == [{"role": "user", "content": "the prompt"}]
    assert body["reasoning_effort"] == "max"
    assert "tools" not in body and "functions" not in body  # no trade-capable tools
    assert p.last_allowance == Decimal("0.00012")


def test_one_session_id_per_adapter_for_prompt_caching() -> None:
    sessions: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sessions.append(req.headers["x-opencode-session"])
        return _ok()

    p = _provider(handler)
    p.complete("a", timeout_s=30)
    p.complete("b", timeout_s=30)
    assert len(set(sessions)) == 1


def test_no_effort_sends_no_reasoning_field() -> None:
    bodies: list[dict] = []
    p = _provider(lambda r: (bodies.append(json.loads(r.content)), _ok())[1], effort=None)
    p.complete("x", timeout_s=30)
    assert "reasoning_effort" not in bodies[0]


def test_a_rate_limit_waits_as_told_then_retries_once() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": "slow down"})
        return _ok("after wait")

    slept: list[float] = []
    assert _provider(handler, sleeps=slept).complete("x", timeout_s=30) == "after wait"
    assert slept == [3.0] and calls["n"] == 2


def test_a_rate_limit_longer_than_the_time_left_fails_without_waiting() -> None:
    slept: list[float] = []
    p = _provider(lambda r: httpx.Response(429, headers={"Retry-After": "120"}), sleeps=slept)
    with pytest.raises(ProviderError, match="rate limited"):
        p.complete("x", timeout_s=30)
    assert slept == []


def test_a_second_rate_limit_is_an_error() -> None:
    p = _provider(lambda r: httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(ProviderError, match="rate limited"):
        p.complete("x", timeout_s=30)


def test_a_timeout_is_a_provider_timeout() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ProviderTimeout):
        _provider(handler).complete("x", timeout_s=30)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(403, json={"error": {"message": "Model access is disabled for wrk_ABC123"}}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    ],
)
def test_any_bad_response_is_a_provider_error_without_echoing_it(response: httpx.Response) -> None:
    with pytest.raises(ProviderError) as err:
        _provider(lambda r: response).complete("x", timeout_s=30)
    text = str(err.value)
    assert "wrk_" not in text and "boom" not in text and KEY not in text


def test_a_transport_failure_is_a_provider_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ProviderError):
        _provider(handler).complete("x", timeout_s=30)


def test_a_missing_key_fails_before_any_request() -> None:
    seen: list[httpx.Request] = []
    p = OpenAICompatibleProvider(
        base_url=BASE, model="m", api_key_ref="env:LLM_KEY", env={},
        transport=httpx.MockTransport(lambda r: (seen.append(r), _ok())[1]),
    )  # fmt: skip
    with pytest.raises(ProviderError, match="key"):
        p.complete("x", timeout_s=30)
    assert seen == []


def test_the_key_never_appears_in_the_repr() -> None:
    assert KEY not in repr(_provider(lambda r: _ok()))


def test_config_takes_an_https_base_url_and_an_effort() -> None:
    cfg = LlmConfig(provider="opencode-go", model="qwen3.8-flash", base_url=BASE,
                    reasoning_effort="xhigh", api_key_ref="env:LLM_KEY")  # fmt: skip
    assert cfg.base_url == BASE
    with pytest.raises(ValidationError):
        LlmConfig(base_url="http://plain.example/v1")
    with pytest.raises(ValidationError):
        LlmConfig(reasoning_effort="max; drop")


def test_the_provider_is_built_from_config_only_when_complete() -> None:
    full = UserConfig.model_validate({"llm": {
        "provider": "opencode-go", "model": "qwen3.8-flash", "base_url": BASE,
        "reasoning_effort": "xhigh", "api_key_ref": "env:LLM_KEY"}}).llm  # fmt: skip
    p = provider_from_config(full, env=ENV)
    assert isinstance(p, OpenAICompatibleProvider) and p.model == "qwen3.8-flash"
    assert p.provider_id == "opencode-go"
    assert provider_from_config(full.model_copy(update={"base_url": None}), env=ENV) is None
    assert provider_from_config(full.model_copy(update={"model": None}), env=ENV) is None
    assert provider_from_config(full.model_copy(update={"api_key_ref": None}), env=ENV) is None
    assert provider_from_config(full.model_copy(update={"news_handling": "off"}), env=ENV) is None


def test_separate_adapters_use_separate_sessions() -> None:
    """A session groups one conversation for routing and caching; two adapters (e.g. a
    primary and a fallback model) must not share one."""
    sessions: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sessions.append(req.headers["x-opencode-session"])
        return _ok()

    _provider(handler).complete("a", timeout_s=30)
    _provider(handler).complete("a", timeout_s=30)
    assert len(set(sessions)) == 2
