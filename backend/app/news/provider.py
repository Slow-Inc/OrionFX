"""LLM provider port (#39; LLM-01, LLM-05).

A provider turns a prompt into text, within a timeout. The port has no tools,
functions or broker access by construction: there is no parameter to pass one
(M03 acceptance "no trade-capable tools exposed"). The API key is configured as
a `SecretRef` and resolved by the real adapter only (#41, blocked: no key here);
nothing in M03 holds the key itself. A deterministic, scriptable fake implements
the port for tests and until a key exists.
"""

from collections.abc import Sequence
from typing import Protocol


class ProviderError(Exception):
    pass


class ProviderTimeout(ProviderError):
    pass


class Provider(Protocol):
    provider_id: str
    model: str

    def complete(self, prompt: str, *, timeout_s: float) -> str: ...


class FakeProvider:
    """Replays a script of answers or exceptions, one per call; records prompts."""

    provider_id = "fake"
    model = "fake-1"

    def __init__(self, script: Sequence[str | ProviderError]) -> None:
        self._script = list(script)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        self.prompts.append(prompt)
        if not self._script:
            raise ProviderError("fake provider script exhausted")
        step = self._script.pop(0)
        if isinstance(step, ProviderError):
            raise step
        return step
