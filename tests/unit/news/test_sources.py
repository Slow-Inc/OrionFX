"""M03 slice A: news sources, provenance, cache, dedup and schedule (#38; LLM-02, LLM-03,
LLM-05). Repeats never become independent evidence; expired items are dropped;
fetching follows the calendar and events, never every tick."""

from datetime import UTC, datetime, timedelta

import pytest

from app.news.cache import NewsCache, fingerprint
from app.news.schedule import CalendarEvent, FetchPolicy, should_fetch
from app.news.sources import FixtureSource, NewsItem, NoSource

T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _item(
    i: str, title: str, *, source: str = "wire-a", at: datetime = T0, body: str = ""
) -> NewsItem:
    return NewsItem(
        item_id=i,
        source=source,
        url=f"https://{source}.example/{i}",
        title=title,
        body=body or f"{title}. Gold traders react.",
        published_at=at,
        fetched_at=at + timedelta(minutes=1),
        valid_until=at + timedelta(hours=6),
    )


# --- sources --------------------------------------------------------------------------------------


def test_fixture_source_returns_items_after_a_time() -> None:
    src = FixtureSource(
        [_item("1", "Fed holds"), _item("2", "CPI hot", at=T0 + timedelta(hours=1))]
    )
    assert [i.item_id for i in src.fetch(since=T0)] == ["2"]
    assert src.available


def test_no_source_says_so_instead_of_claiming_news() -> None:
    src = NoSource()
    assert not src.available
    assert src.fetch(since=T0) == []


def test_item_needs_provenance() -> None:
    with pytest.raises(ValueError):
        NewsItem(
            item_id="x", source="", url="https://a.example/x", title="t", body="b",
            published_at=T0, fetched_at=T0, valid_until=T0 + timedelta(hours=1),
        )  # fmt: skip
    with pytest.raises(ValueError):  # fetched before it was published
        NewsItem.model_validate(_item("y", "t").model_dump() | {"fetched_at": T0 - timedelta(1)})


# --- dedup ----------------------------------------------------------------------------------------


def test_the_same_story_from_two_wires_is_one_item_with_two_sources() -> None:
    cache = NewsCache()
    first = _item("1", "Fed holds rates steady", source="wire-a")
    repeat = _item("9", "FED HOLDS rates  steady!", source="wire-b")
    assert cache.add(first) is True
    assert cache.add(repeat) is False  # not new evidence
    (only,) = cache.current(T0)
    assert only.item_id == "1" and only.sources == ("wire-a", "wire-b")


def test_same_url_is_a_duplicate_even_with_an_edited_title() -> None:
    cache = NewsCache()
    a = _item("1", "Gold jumps")
    b = a.model_copy(update={"item_id": "2", "title": "Gold jumps 2%"})
    cache.add(a)
    assert cache.add(b) is False


def test_different_stories_are_kept_apart() -> None:
    cache = NewsCache()
    cache.add(_item("1", "Fed holds rates"))
    cache.add(_item("2", "CPI beats forecast"))
    assert {i.item_id for i in cache.current(T0)} == {"1", "2"}


def test_expired_items_are_dropped() -> None:
    cache = NewsCache()
    cache.add(_item("1", "Old news", at=T0 - timedelta(hours=7)))
    cache.add(_item("2", "Fresh news"))
    assert [i.item_id for i in cache.current(T0)] == ["2"]


def test_fingerprint_ignores_case_spacing_and_punctuation() -> None:
    assert fingerprint("Fed  HOLDS, rates!", "x") == fingerprint("fed holds rates", "x")
    assert fingerprint("Fed holds", "a") != fingerprint("Fed cuts", "a")


# --- schedule -------------------------------------------------------------------------------------


POLICY = FetchPolicy(base_interval=timedelta(minutes=30), event_window=timedelta(minutes=15),
                     event_interval=timedelta(minutes=2))  # fmt: skip
NFP = CalendarEvent(name="NFP", at=T0 + timedelta(hours=2), impact="high")


def test_base_interval_between_events() -> None:
    assert not should_fetch(
        T0 + timedelta(minutes=10), last_fetch=T0, calendar=[NFP], policy=POLICY
    )
    assert should_fetch(T0 + timedelta(minutes=30), last_fetch=T0, calendar=[NFP], policy=POLICY)


def test_tighter_interval_around_a_high_impact_event() -> None:
    near = NFP.at - timedelta(minutes=10)
    assert should_fetch(near, last_fetch=near - timedelta(minutes=3), calendar=[NFP], policy=POLICY)
    after = NFP.at + timedelta(minutes=5)
    assert should_fetch(
        after, last_fetch=after - timedelta(minutes=2), calendar=[NFP], policy=POLICY
    )
    low = CalendarEvent(name="minor", at=NFP.at, impact="low")
    assert not should_fetch(
        near, last_fetch=near - timedelta(minutes=3), calendar=[low], policy=POLICY
    )


def test_never_fetches_every_tick() -> None:
    fetches = 0
    last = T0
    for s in range(0, 4 * 3600, 1):  # one "tick" per second for four hours
        now = T0 + timedelta(seconds=s)
        if should_fetch(now, last_fetch=last, calendar=[NFP], policy=POLICY):
            fetches += 1
            last = now
    assert fetches < 40  # 8 base fetches + ~15 around NFP, not 14,400


def test_first_fetch_happens_immediately() -> None:
    assert should_fetch(T0, last_fetch=None, calendar=[], policy=POLICY)
