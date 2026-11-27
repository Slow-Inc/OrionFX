"""News source port and provenance (#38; LLM-02, LLM-03, LLM-05).

Every item carries where it came from (source, URL), when it was published and
fetched, and when it stops being relevant. A fixture source implements the port
now; real news/calendar adapters are blocked (no licensed source, #41).
`NoSource` is the honest default: it is not available and returns nothing, so
nothing downstream can claim news access that does not exist.

`NewsItem` is internal to M03: only `news.assessment` (which cites item ids and
source refs) crosses the module boundary.

The read cursor is `fetched_at`, not `published_at`: a story published long ago
but delivered late (backfill, slow wire) is still returned once.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, Self

from pydantic import Field, model_validator

from app.core.contracts import ContractModel
from app.core.time import CLOCK_SKEW, require_utc
from app.core.types import UtcDatetime


class NewsItem(ContractModel):
    item_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    """A safe character set: the id is the only feed value placed in a prompt outside
    the untrusted blocks."""
    source: str = Field(min_length=1)
    url: str = Field(pattern=r"^https?://")
    title: str = Field(min_length=1)
    body: str
    published_at: UtcDatetime
    fetched_at: UtcDatetime
    valid_until: UtcDatetime
    sources: tuple[str, ...] = ()
    """Every source that carried this story, its own `source` first."""

    @model_validator(mode="before")
    @classmethod
    def _default_sources(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("sources") and data.get("source"):
            data = {**data, "sources": (data["source"],)}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        # The publisher's clock may run a little ahead of ours.
        if not (
            self.published_at <= self.fetched_at + CLOCK_SKEW and self.fetched_at < self.valid_until
        ):
            raise ValueError("need published_at <= fetched_at (+ clock skew) < valid_until")
        if not self.sources or self.sources[0] != self.source:
            raise ValueError("sources must start with the item's own source")
        return self


class NewsSource(Protocol):
    @property
    def available(self) -> bool: ...

    def fetch(self, *, since: datetime) -> list[NewsItem]:
        """Items fetched after `since`, oldest fetch first."""
        ...


class FixtureSource:
    def __init__(self, items: Sequence[NewsItem]) -> None:
        self._items = sorted(items, key=lambda i: i.fetched_at)

    @property
    def available(self) -> bool:
        return True

    def fetch(self, *, since: datetime) -> list[NewsItem]:
        require_utc(since)
        return [i for i in self._items if i.fetched_at > since]


class NoSource:
    """No news source configured (the default until one is licensed)."""

    @property
    def available(self) -> bool:
        return False

    def fetch(self, *, since: datetime) -> list[NewsItem]:
        return []
