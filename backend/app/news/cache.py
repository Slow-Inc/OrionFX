"""News cache and de-duplication (#38; LLM-03, M03 acceptance "duplicate news not
treated as independent evidence").

A story is identified by its normalized URL and, when its body is long enough to
tell stories apart, by a content fingerprint (title and the start of the body,
lower-cased with punctuation and spacing collapsed). A repeat is merged into the
first item as an extra source and extends its validity; it never becomes a
separate item, so a story syndicated ten times counts once. Items are keyed by
(source, item_id), so two sources' ids never collide. Expired stories are pruned
before every add and read; a repeat arriving after its story expired counts as
new, because the earlier evidence is no longer in force.
"""

import hashlib
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from app.core.time import require_utc
from app.news.sources import NewsItem

BODY_PREFIX = 200  # characters of body in the fingerprint [PROPOSED]
MIN_FINGERPRINT_BODY = 40  # shorter bodies dedup by URL only [PROPOSED]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def fingerprint(title: str, body: str) -> str:
    head = _norm(body[: BODY_PREFIX * 2])[:BODY_PREFIX]
    return hashlib.sha256(f"{_norm(title)}|{head}".encode()).hexdigest()


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _keys(item: NewsItem) -> list[str]:
    keys = [f"url:{normalize_url(item.url)}"]
    if len(_norm(item.body)) >= MIN_FINGERPRINT_BODY:  # a bare headline is too generic
        keys.append(f"fp:{fingerprint(item.title, item.body)}")
    return keys


class NewsCache:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], NewsItem] = {}
        self._by_key: dict[str, tuple[str, str]] = {}

    def _prune(self, now: datetime) -> None:
        require_utc(now)
        dead = [k for k, it in self._items.items() if it.valid_until <= now]
        for k in dead:
            del self._items[k]
        if dead:
            self._by_key = {k: v for k, v in self._by_key.items() if v in self._items}

    def add(self, item: NewsItem) -> bool:
        """True when the item is a new story; False when it repeats a live one."""
        self._prune(item.fetched_at)
        keys = _keys(item)
        known = next((self._by_key[k] for k in keys if k in self._by_key), None)
        if known is None:
            ident = (item.source, item.item_id)
            if ident in self._items:
                return False  # the same item delivered again
            self._items[ident] = item
            for k in keys:
                self._by_key[k] = ident
            return True
        first = self._items[known]
        self._items[known] = first.model_copy(
            update={
                "sources": first.sources
                + ((item.source,) if item.source not in first.sources else ()),
                "valid_until": max(first.valid_until, item.valid_until),
            }
        )
        for k in keys:
            self._by_key.setdefault(k, known)
        return False

    def current(self, now: datetime) -> list[NewsItem]:
        """Unexpired stories, oldest first."""
        self._prune(now)
        return sorted(self._items.values(), key=lambda i: i.published_at)
