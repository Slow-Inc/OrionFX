"""M03 off the decision tick (#101, PRD #98): a news worker assesses and reasons with a long
timeout and journals the results; the decision loop reads evidence from the journal and
never calls the LLM. Evidence is usable only from when the answer returned (no look-ahead)."""

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from tests.support.engine import Clock

from app.core.config import LlmConfig, UserConfig
from app.core.contracts import HealthUpdated, LlmReasoning, NewsAssessment
from app.core.enums import Direction, HealthStatus
from app.news.provider import FakeProvider, ProviderError
from app.news.sources import FixtureSource, NewsItem
from app.runtime.news_worker import NewsWorker, journal_evidence
from app.storage.config_store import ConfigStore
from app.storage.db import connect
from app.storage.journal import Journal, payload_as

ASSESS = json.dumps({
    "direction": "DOWN", "impact": 0.7, "confidence": 0.8, "cited_ids": ["n1"],
    "item_directions": {"n1": "DOWN"}, "rationale": "Hot CPI lifts yields.",
})  # fmt: skip


def _reasoning_answer(prompt: str) -> str:
    aid = prompt.split("Citable assessment ids: ")[1].split("\n")[0]
    return json.dumps({"direction": "DOWN", "confidence": 0.6, "cited_assessment_ids": [aid],
                       "cited_prediction_ids": [], "rationale": "News is bearish."})  # fmt: skip


class SlowProvider(FakeProvider):
    """Answers assessment prompts from a script, reasoning prompts by citing what it was
    given, and lets `seconds` pass on the clock during every call."""

    provider_id = "fake"
    model = "fake-1"

    def __init__(self, clock: Clock, script: Sequence[str | ProviderError], seconds: float = 40):
        super().__init__(script)
        self.clock, self.seconds, self.closed = clock, seconds, False

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        self.clock.advance(self.seconds)
        if "Citable assessment ids" in prompt:
            self.prompts.append(prompt)
            return _reasoning_answer(prompt)
        return super().complete(prompt, timeout_s=timeout_s)

    def close(self) -> None:
        self.closed = True


def _item(clock: Clock, i: str = "n1",
          title: str = "US CPI rises 0.6% m/m, well above forecast") -> NewsItem:  # fmt: skip
    now = clock()
    return NewsItem(item_id=i, source="wire", url=f"https://wire.example/{i}", title=title,
                    body="Two-year yields surged and the dollar rallied.",
                    published_at=now - timedelta(minutes=2), fetched_at=now - timedelta(minutes=1),
                    valid_until=now + timedelta(hours=2))  # fmt: skip


class ListSource(FixtureSource):
    """A source whose list the test can grow between ticks."""

    def __init__(self, items: list[NewsItem]) -> None:
        self._live = items

    def fetch(self, *, since: datetime) -> list[NewsItem]:
        return [i for i in self._live if i.fetched_at > since]


def _worker(conn: sqlite3.Connection, clock: Clock, provider: FakeProvider | None,
            items: list[NewsItem]) -> NewsWorker:  # fmt: skip
    return NewsWorker(conn, source=ListSource(items), providers=lambda cfg: provider,
                      clock=clock)  # fmt: skip


def _assessments(conn: sqlite3.Connection) -> list[NewsAssessment]:
    return [payload_as(e, NewsAssessment) for e in Journal(conn).recent(["news.assessment"])]


def test_an_assessment_is_journaled_and_usable_only_from_when_it_returned(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    provider = SlowProvider(clock, [ASSESS])
    start = clock()
    _worker(conn, clock, provider, [_item(clock)]).tick()
    (a,) = _assessments(conn)
    assert a.direction is Direction.DOWN and a.available_at == start + timedelta(seconds=40)
    (ev,) = Journal(conn).recent(["news.assessment"])
    assert ev.envelope.occurred_at == a.available_at  # the worker's clock, not wall time
    evidence = journal_evidence(conn)
    assert evidence(start).news == []  # the tick that started the call cannot see it
    assert evidence(clock()).news == [a]


def test_the_worker_reasons_about_the_assessment_it_just_wrote(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    _worker(conn, clock, SlowProvider(clock, [ASSESS]), [_item(clock)]).tick()
    (a,) = _assessments(conn)
    (ev,) = Journal(conn).recent(["llm.reasoning"])
    r = payload_as(ev, LlmReasoning)
    assert r.news_assessment_ids == [a.assessment_id] and r.available_at > a.available_at
    assert journal_evidence(conn)(clock()).reasoning == r


def test_reading_evidence_never_calls_the_provider(conn: sqlite3.Connection, clock: Clock) -> None:
    provider = SlowProvider(clock, [ASSESS])
    _worker(conn, clock, provider, [_item(clock)]).tick()
    calls = len(provider.prompts)
    for _ in range(5):
        journal_evidence(conn)(clock())
    assert len(provider.prompts) == calls


def test_only_the_newest_live_assessment_counts(conn: sqlite3.Connection, clock: Clock) -> None:
    """Each assessment covers the whole current news set, so an older one must not add a
    second vote (e.g. one written before a correction arrived)."""
    up = ASSESS.replace('"DOWN"', '"UP"')
    items = [_item(clock, "n1")]
    worker = _worker(conn, clock, SlowProvider(clock, [up, ASSESS], seconds=1), items)
    worker.tick()
    clock.advance(31 * 60)  # the next fetch window
    items.append(_item(clock, "n2", "Fed official says rate cuts remain off the table"))
    worker.tick()
    news = journal_evidence(conn)(clock()).news
    assert len(news) == 1 and news[0].direction is Direction.DOWN


def test_a_failed_assessment_is_retried_after_a_backoff_not_every_tick(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    fail = ProviderError("down")
    provider = SlowProvider(clock, [fail, fail, ASSESS], seconds=1)
    worker = _worker(conn, clock, provider, [_item(clock)])
    worker.tick()
    assert _assessments(conn) == [] and len(provider.prompts) == 2  # ask() tried twice
    clock.advance(10)
    worker.tick()  # inside the backoff: no new call
    assert len(provider.prompts) == 2
    clock.advance(120)
    worker.tick()  # the same, no longer new, items are assessed again
    assert len(_assessments(conn)) == 1


def test_no_provider_reports_down_and_writes_no_evidence(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    _worker(conn, clock, None, [_item(clock)]).tick()
    assert _assessments(conn) == []
    (h,) = Journal(conn).recent(["health.updated"])
    assert payload_as(h, HealthUpdated).status is HealthStatus.DOWN


def test_idle_ticks_journal_no_health(conn: sqlite3.Connection, clock: Clock) -> None:
    worker = _worker(conn, clock, None, [_item(clock)])
    for _ in range(5):
        worker.tick()
        clock.advance(5)
    assert len(Journal(conn).recent(["health.updated"])) == 1


def test_a_config_change_rebuilds_the_provider_and_closes_the_old_one(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    built: list[SlowProvider] = []

    def providers(cfg: LlmConfig) -> SlowProvider:
        built.append(SlowProvider(clock, [ASSESS], seconds=1))
        return built[-1]

    worker = NewsWorker(conn, source=FixtureSource([_item(clock)]), providers=providers,
                        clock=clock)  # fmt: skip
    worker.tick()
    worker.tick()
    assert len(built) == 1
    v, cfg = ConfigStore(conn).current()
    new = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"model": "other"})})
    ConfigStore(conn).update(UserConfig.model_validate(new.model_dump()), expected_version=v,
                             actor="test")  # fmt: skip
    worker.tick()
    assert len(built) == 2 and built[0].closed
    worker.close()
    assert built[1].closed


def test_the_worker_and_the_loop_can_use_separate_connections(
    conn: sqlite3.Connection, clock: Clock, tmp_path: Path
) -> None:
    worker_conn = connect(tmp_path / "engine.db")  # the same database file as `conn`
    try:
        _worker(worker_conn, clock, SlowProvider(clock, [ASSESS]), [_item(clock)]).tick()
    finally:
        worker_conn.close()
    assert len(journal_evidence(conn)(clock()).news) == 1


def test_news_is_fetched_on_its_schedule_not_every_tick(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    class Counting(FixtureSource):
        calls = 0

        def fetch(self, *, since: datetime) -> list[NewsItem]:
            Counting.calls += 1
            return super().fetch(since=since)

    worker = NewsWorker(conn, source=Counting([_item(clock)]),
                        providers=lambda c: SlowProvider(clock, [ASSESS], seconds=1),
                        clock=clock)  # fmt: skip
    for _ in range(4):
        worker.tick()
        clock.advance(60)
    assert Counting.calls == 1  # base interval is 30 min


def test_reasoning_about_an_older_assessment_is_not_used(
    conn: sqlite3.Connection, clock: Clock
) -> None:
    """Fusion counts reasoning only with the news it cites; the evidence reader must not
    pair the newest assessment with reasoning written about an earlier one."""
    items = [_item(clock, "n1")]
    worker = _worker(conn, clock, SlowProvider(clock, [ASSESS, ASSESS], seconds=1), items)
    worker.tick()
    clock.advance(31 * 60)
    items.append(_item(clock, "n2", "Fed official says rate cuts remain off the table"))
    worker._reason = lambda assessment, provider: None  # type: ignore[method-assign]
    worker.tick()  # a new assessment, but no reasoning about it
    ev = journal_evidence(conn)(clock())
    assert len(ev.news) == 1 and ev.reasoning is None
