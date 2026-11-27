"""News cache and de-duplication (#38; LLM-03, M03 acceptance "duplicate news not
treated as independent evidence").

A story is identified by its URL and by a content fingerprint (title and the
start of the body, lower-cased with punctuation and spacing collapsed). A repeat
is merged into the first item as an extra source; it never becomes a separate
item, so the same wire story syndicated ten times counts once. Expired items are
not returned.
"""

import hashlib
import re
from datetime import datetime

from app.news.sources import NewsItem

BODY_PREFIX = 200  # characters of body in the fingerprint [PROPOSED]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def fingerprint(title: str, body: str) -> str:
    return hashlib.sha256(f"{_norm(title)}|{_norm(body)[:BODY_PREFIX]}".encode()).hexdigest()


class NewsCache:
    def __init__(self) -> None:
        self._items: dict[str, NewsItem] = {}
        self._by_key: dict[str, str] = {}

    def add(self, item: NewsItem) -> bool:
        """True when the item is a new story; False when it repeats a known one."""
        keys = (f"url:{item.url}", f"fp:{fingerprint(item.title, item.body)}")
        known = next((self._by_key[k] for k in keys if k in self._by_key), None)
        if known is not None:
            first = self._items[known]
            if item.source not in first.sources:
                self._items[known] = first.model_copy(
                    update={"sources": (*first.sources, item.source)}
                )
            for k in keys:
                self._by_key.setdefault(k, known)
            return False
        self._items[item.item_id] = item.model_copy(update={"sources": (item.source,)})
        for k in keys:
            self._by_key[k] = item.item_id
        return True

    def current(self, now: datetime) -> list[NewsItem]:
        """Unexpired stories, oldest first."""
        live = [i for i in self._items.values() if i.valid_until > now]
        return sorted(live, key=lambda i: i.published_at)
