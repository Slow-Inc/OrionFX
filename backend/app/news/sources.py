"""News source port and provenance (#38; LLM-02, LLM-03, LLM-05).

Every item carries where it came from (source, URL), when it was published and
fetched, and when it stops being relevant. A fixture source implements the port
now; real news/calendar adapters are blocked (no licensed source, #41).
`NoSource` is the honest default: it is not available and returns nothing, so
nothing downstream can claim news access that does not exist.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.core.contracts import ContractModel
from app.core.types import UtcDatetime


class NewsItem(ContractModel):
    item_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    url: str = Field(pattern=r"^https?://")
    title: str = Field(min_length=1)
    body: str
    published_at: UtcDatetime
    fetched_at: UtcDatetime
    valid_until: UtcDatetime
    sources: tuple[str, ...] = ()
    """Every source that carried this story (the first item's source first)."""

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not (self.published_at <= self.fetched_at < self.valid_until):
            raise ValueError("need published_at <= fetched_at < valid_until")
        return self


class NewsSource(Protocol):
    @property
    def available(self) -> bool: ...

    def fetch(self, *, since: datetime) -> list[NewsItem]:
        """Items published after `since`, oldest first."""
        ...


class FixtureSource:
    def __init__(self, items: Sequence[NewsItem]) -> None:
        self._items = sorted(items, key=lambda i: i.published_at)

    @property
    def available(self) -> bool:
        return True

    def fetch(self, *, since: datetime) -> list[NewsItem]:
        return [i for i in self._items if i.published_at > since]


class NoSource:
    """No news source configured (the default until one is licensed)."""

    @property
    def available(self) -> bool:
        return False

    def fetch(self, *, since: datetime) -> list[NewsItem]:
        return []
