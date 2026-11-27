"""M03 off the decision tick (#101, PRD #98; LLM-02, LLM-04, AT-15).

A real LLM answer can take a minute at high reasoning effort, so M03 must never run
inside the decision loop's tick. `NewsWorker.tick()` runs on its own schedule, on its own
connection:
- it fetches news when `should_fetch` says so and caches it;
- when new stories arrived, or a failed assessment is due for a retry, it assesses the
  current news set with a long timeout and journals `news.assessment`;
- it then reasons about that assessment (with the newest live forecast and feature
  snapshot from the journal, when any) and journals `llm.reasoning`;
- it journals M03 health whenever a stage ran (idle ticks write nothing).
Everything is stamped from the worker's clock when the answer returned, so evidence is
never usable before it existed.

`journal_evidence(conn)` is what the decision loop reads: the newest live assessment
(each one covers the whole current news set, so an older one must not add a vote), the
newest live reasoning that cites it, and the newest live forecast. It never calls a
provider.

The provider is built from the owner's LLM config and rebuilt (the old one closed) when
the config version changes. Wiring the worker into `serve` waits on a licensed news
source and the MT5 adapter (#17).
"""

import logging
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

from app.core.config import LlmConfig
from app.core.contracts import (
    FeatureSnapshot,
    HealthUpdated,
    LlmReasoning,
    NewsAssessment,
    PredictionGenerated,
)
from app.core.enums import HealthStatus, Mode
from app.core.events import make_event
from app.core.ids import stable_id
from app.core.time import Clock, live, utc_now
from app.decision.fusion import Evidence
from app.news.assess import assess_news
from app.news.cache import NewsCache
from app.news.openai_compat import provider_from_config
from app.news.provider import Provider
from app.news.reasoning import reason
from app.news.schedule import CalendarEvent, FetchPolicy, should_fetch
from app.news.sources import NewsSource
from app.runtime.loop import EvidenceProvider
from app.storage.config_store import ConfigStore
from app.storage.journal import Journal, payload_as

__all__ = ["WORKER_TIMEOUT_S", "NewsWorker", "journal_evidence"]

WORKER_TIMEOUT_S = 45.0
"""Per attempt. M03 makes at most two attempts per stage, so one stage can take up to
~90 s plus one rate-limit wait inside the deadline [PROPOSED]."""
RETRY_FAILED_AFTER = timedelta(minutes=2)
"""A failed assessment is retried after this, never on every tick [PROPOSED]."""
FIRST_LOOKBACK = timedelta(hours=6)
"""How far back the first fetch reaches [PROPOSED]."""
SOURCE = "m03"
SCAN = 50

log = logging.getLogger(__name__)


class NewsWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        source: NewsSource,
        providers: Callable[[LlmConfig], Provider | None] = provider_from_config,
        calendar: Callable[[], Sequence[CalendarEvent]] = lambda: (),
        policy: FetchPolicy = FetchPolicy(),  # noqa: B008  (frozen dataclass)
        clock: Clock = utc_now,
        timeout_s: float = WORKER_TIMEOUT_S,
        account_id: str = "local-account",
        bot_id: str = "bot-1",
    ) -> None:
        self._conn, self._journal = conn, Journal(conn)
        self._source, self._providers, self._calendar = source, providers, calendar
        self._policy, self._clock, self._timeout_s = policy, clock, timeout_s
        self._account_id, self._bot_id = account_id, bot_id
        self._cache = NewsCache()
        self._last_fetch: datetime | None = None
        self._pending = False
        self._retry_at: datetime | None = None
        self._provider: tuple[int, Provider | None] | None = None
        self._mode = Mode.SHADOW

    def close(self) -> None:
        if self._provider is not None:
            _close(self._provider[1])
            self._provider = None

    def tick(self) -> None:
        now = self._clock()
        provider = self._current_provider()
        self._fetch(now)
        if not self._pending or (self._retry_at is not None and now < self._retry_at):
            return
        result = assess_news(self._cache.current(now), provider, now=now,
                             timeout_s=self._timeout_s, clock=self._clock)  # fmt: skip
        self._health(result.health)
        if result.assessment is None:
            failed = result.health.status is not HealthStatus.OK
            self._retry_at = self._clock() + RETRY_FAILED_AFTER if failed else None
            self._pending = failed
            return
        self._pending, self._retry_at = False, None
        assessment = result.assessment
        self._append("news.assessment", assessment, assessment.assessment_id,
                     assessment.available_at)  # fmt: skip
        self._reason(assessment, provider)

    # --- steps --------------------------------------------------------------------------

    def _current_provider(self) -> Provider | None:
        version, cfg = ConfigStore(self._conn).current()
        self._mode = Mode(cfg.trading.account_mode)
        if self._provider is None or self._provider[0] != version:
            self.close()
            self._provider = (version, self._providers(cfg.llm))
        return self._provider[1]

    def _fetch(self, now: datetime) -> None:
        if not self._source.available or not should_fetch(
            now, last_fetch=self._last_fetch, calendar=self._calendar(), policy=self._policy
        ):
            return
        since = self._last_fetch or now - FIRST_LOOKBACK
        self._last_fetch = now
        for item in self._source.fetch(since=since):
            if self._cache.add(item):
                self._pending = True

    def _reason(self, assessment: NewsAssessment, provider: Provider | None) -> None:
        now = self._clock()
        forecast = _newest_live(self._journal, "prediction.generated", PredictionGenerated, now,
                                lambda p: (p.generated_at, p.valid_until))  # fmt: skip
        snapshot = _newest_live(self._journal, "feature.snapshot", FeatureSnapshot, now,
                                lambda s: (s.available_at, s.valid_until))  # fmt: skip
        result = reason([assessment], [forecast] if forecast else [], snapshot, provider,
                        now=now, timeout_s=self._timeout_s, clock=self._clock)  # fmt: skip
        self._health(result.health)
        if result.reasoning is not None:
            self._append("llm.reasoning", result.reasoning, result.reasoning.reasoning_id,
                         result.reasoning.available_at)  # fmt: skip

    # --- journal ------------------------------------------------------------------------

    def _append(
        self, kind: str, payload: NewsAssessment | LlmReasoning, key: str, at: datetime
    ) -> None:
        self._journal.append(make_event(
            kind, source=SOURCE, payload=payload, account_id=self._account_id,
            bot_id=self._bot_id, mode=self._mode, event_id=stable_id("evt", kind, key),
            occurred_at=at,
        ))  # fmt: skip

    def _health(self, h: HealthUpdated) -> None:
        """M03 health, journaled only when a stage ran (idle ticks write nothing: the
        fetch schedule and the retry backoff already bound how often that is)."""
        now = h.last_heartbeat
        try:
            self._journal.append(make_event(
                "health.updated", source=SOURCE, payload=h, account_id=self._account_id,
                bot_id=self._bot_id, mode=self._mode,
                event_id=stable_id("evt", h.component, now, h.status, h.detail),
                occurred_at=now,
            ))  # fmt: skip
        except Exception:  # health is best effort; never let it stop the worker
            log.exception("M03 worker could not report health %s", h.status.value)


def _close(provider: Provider | None) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def _newest_live[T](
    journal: Journal,
    kind: str,
    model: type[T],
    now: datetime,
    window: Callable[[T], tuple[datetime, datetime]],
) -> T | None:
    for ev in journal.recent([kind], limit=SCAN):  # newest first
        item = payload_as(ev, model)  # type: ignore[type-var]
        if live(*window(item), now):
            return item
    return None


def journal_evidence(conn: sqlite3.Connection) -> EvidenceProvider:
    """Evidence for the decision loop, read from the journal only (no LLM call)."""
    journal = Journal(conn)

    def evidence(now: datetime) -> Evidence:
        news = _newest_live(journal, "news.assessment", NewsAssessment, now,
                            lambda a: (a.available_at, a.valid_until))  # fmt: skip
        reasoning = None
        if news is not None:
            for ev in journal.recent(["llm.reasoning"], limit=SCAN):
                r = payload_as(ev, LlmReasoning)
                if news.assessment_id in r.news_assessment_ids and live(
                    r.available_at, r.valid_until, now
                ):
                    reasoning = r
                    break
        forecast = _newest_live(journal, "prediction.generated", PredictionGenerated, now,
                                lambda p: (p.generated_at, p.valid_until))  # fmt: skip
        return Evidence(forecast=forecast, news=[news] if news else [], reasoning=reasoning)

    return evidence
