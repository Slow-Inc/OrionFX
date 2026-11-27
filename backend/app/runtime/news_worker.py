"""M03 off the decision tick (#101, PRD #98; LLM-02, LLM-04, AT-15).

A real LLM answer can take a minute at high reasoning effort, so M03 must never run
inside the decision loop's tick. `NewsWorker.tick()` runs on its own schedule, on its own
connection:
- it fetches news when `should_fetch` says so and caches it;
- it assesses the live cached news (long timeout) when a new story arrived, when the
  newest assessment expired while news is still live, or when a failed attempt is due
  for its retry, and journals `news.assessment`;
- it reasons about that assessment (with the newest live forecast and feature snapshot
  from the journal, when any) and journals `llm.reasoning`; a failed reasoning is retried
  on its own;
- it journals M03 health whenever a stage ran (idle ticks write nothing).
Everything is stamped from the worker's clock when the answer returned, so evidence is
never usable before it existed. A failure anywhere in a tick is reported and backed off;
`tick()` never raises.

`journal_evidence(conn)` is what the decision loop reads, refusal first: the newest
assessment already available at `now` if it is still live (an older one is a stale view,
so none is returned rather than an older one); reasoning only if it cites that
assessment and no forecast other than the one returned; and the newest live forecast. It
never calls a provider.

The provider is built from the owner's LLM config and rebuilt (the old one closed) when
that config changes. Wiring the worker into `serve` waits on a licensed news source and
the MT5 adapter (#17).
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
from app.news.llm_call import COMPONENT
from app.news.openai_compat import provider_from_config
from app.news.provider import Provider
from app.news.reasoning import reason
from app.news.schedule import CalendarEvent, FetchPolicy, should_fetch
from app.news.sources import NewsItem, NewsSource
from app.runtime.loop import EvidenceProvider
from app.storage.config_store import ConfigStore
from app.storage.journal import Journal, payload_as

__all__ = ["WORKER_TIMEOUT_S", "NewsWorker", "journal_evidence"]

WORKER_TIMEOUT_S = 45.0
"""Per attempt. M03 makes at most two attempts per stage, so one stage can take up to
~90 s plus one rate-limit wait inside the deadline [PROPOSED]."""
RETRY_AFTER = timedelta(minutes=2)
"""A failed stage (or tick) is retried after this, never on every tick [PROPOSED]."""
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
        self._pending = False  # the live news needs a (new) assessment
        self._unreasoned: NewsAssessment | None = None  # journaled, reasoning not yet done
        self._valid_until: datetime | None = None  # the newest assessment's expiry
        self._retry_at: datetime | None = None
        self._provider: tuple[LlmConfig, Provider | None] | None = None
        self._mode = Mode.SHADOW

    def close(self) -> None:
        if self._provider is not None:
            _close(self._provider[1])
            self._provider = None

    def tick(self) -> None:
        now = self._clock()
        if self._retry_at is not None and now < self._retry_at:
            return
        self._retry_at = None  # the backoff is over; a failing stage sets it again
        try:
            self._tick(now)
        except Exception as exc:  # report and back off; a worker fault never propagates
            log.exception("M03 news worker tick failed")
            self._retry_at = self._clock() + RETRY_AFTER
            self._report(HealthStatus.DEGRADED, f"news worker: {type(exc).__name__}", "tick")

    def _tick(self, now: datetime) -> None:
        provider = self._current_provider()
        self._fetch(now)
        live_news = self._cache.current(now)
        if self._valid_until is not None and now >= self._valid_until and live_news:
            self._pending = True  # the view expired while news is still live
        if self._pending:
            self._assess(now, live_news, provider)
        if self._unreasoned is not None and self._retry_at is None:
            self._reason(self._unreasoned, provider)

    # --- steps --------------------------------------------------------------------------

    def _current_provider(self) -> Provider | None:
        _, cfg = ConfigStore(self._conn).current()
        self._mode = Mode(cfg.trading.account_mode)
        if self._provider is None or self._provider[0] != cfg.llm:
            self.close()
            self._provider = (cfg.llm, self._providers(cfg.llm))
        return self._provider[1]

    def _fetch(self, now: datetime) -> None:
        if not self._source.available or not should_fetch(
            now, last_fetch=self._last_fetch, calendar=self._calendar(), policy=self._policy
        ):
            return
        items = self._source.fetch(since=self._last_fetch or now - FIRST_LOOKBACK)
        self._last_fetch = now  # only once the fetch succeeded: a failed window is retried
        for item in items:
            if self._cache.add(item):
                self._pending = True

    def _assess(self, now: datetime, items: list[NewsItem], provider: Provider | None) -> None:
        result = assess_news(items, provider, now=now, timeout_s=self._timeout_s,
                             clock=self._clock)  # fmt: skip
        self._health(result.health, "assess")
        if result.assessment is None:
            excluded_all = bool(items) and set(result.excluded) == {i.item_id for i in items}
            failed = result.health.status is not HealthStatus.OK and not excluded_all
            self._retry_at = self._clock() + RETRY_AFTER if failed else None
            self._pending = failed
            return
        a = result.assessment
        self._append("news.assessment", a, a.assessment_id, a.available_at)
        self._pending, self._retry_at = False, None  # only after it is journaled
        self._valid_until, self._unreasoned = a.valid_until, a

    def _reason(self, assessment: NewsAssessment, provider: Provider | None) -> None:
        now = self._clock()
        if not live(assessment.available_at, assessment.valid_until, now):
            self._unreasoned = None  # nothing left to reason about
            return
        forecast = _newest_live(self._journal, "prediction.generated", PredictionGenerated, now,
                                lambda p: (p.generated_at, p.valid_until))  # fmt: skip
        snapshot = _newest_live(self._journal, "feature.snapshot", FeatureSnapshot, now,
                                lambda s: (s.available_at, s.valid_until))  # fmt: skip
        result = reason([assessment], [forecast] if forecast else [], snapshot, provider,
                        now=now, timeout_s=self._timeout_s, clock=self._clock)  # fmt: skip
        self._health(result.health, "reason")
        if result.reasoning is None:
            if result.health.status is not HealthStatus.OK:
                self._retry_at = self._clock() + RETRY_AFTER
            else:
                self._unreasoned = None
            return
        r = result.reasoning
        self._append("llm.reasoning", r, r.reasoning_id, r.available_at)
        self._unreasoned = None

    # --- journal ------------------------------------------------------------------------

    def _append(
        self, kind: str, payload: NewsAssessment | LlmReasoning, key: str, at: datetime
    ) -> None:
        self._journal.append(make_event(
            kind, source=SOURCE, payload=payload, account_id=self._account_id,
            bot_id=self._bot_id, mode=self._mode, event_id=stable_id("evt", kind, key),
            occurred_at=at,
        ))  # fmt: skip

    def _report(self, status: HealthStatus, detail: str, stage: str) -> None:
        now = self._clock()
        self._health(HealthUpdated(component=COMPONENT, status=status, last_heartbeat=now,
                                   detail=detail), stage)  # fmt: skip

    def _health(self, h: HealthUpdated, stage: str) -> None:
        """M03 health, journaled when a stage ran (idle ticks write nothing: the fetch
        schedule and the retry backoff bound how often that is). Stamped when written."""
        at = self._clock()
        try:
            self._journal.append(make_event(
                "health.updated", source=SOURCE, payload=h, account_id=self._account_id,
                bot_id=self._bot_id, mode=self._mode,
                event_id=stable_id("evt", h.component, stage, at, h.status, h.detail),
                occurred_at=at,
            ))  # fmt: skip
        except Exception:  # health is best effort; never let it stop the worker
            log.exception("M03 worker could not report health %s", h.status.value)


def _close(provider: Provider | None) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            log.exception("M03 provider did not close cleanly")


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


def _newest_available(journal: Journal, now: datetime) -> NewsAssessment | None:
    """The newest assessment known at `now`, if still live; never an older one."""
    for ev in journal.recent(["news.assessment"], limit=SCAN):
        a = payload_as(ev, NewsAssessment)
        if a.available_at <= now:
            return a if live(a.available_at, a.valid_until, now) else None
    return None


def journal_evidence(conn: sqlite3.Connection) -> EvidenceProvider:
    """Evidence for the decision loop, read from the journal only (no LLM call)."""
    journal = Journal(conn)

    def evidence(now: datetime) -> Evidence:
        news = _newest_available(journal, now)
        forecast = _newest_live(journal, "prediction.generated", PredictionGenerated, now,
                                lambda p: (p.generated_at, p.valid_until))  # fmt: skip
        allowed_forecasts = {forecast.prediction_id} if forecast else set()
        reasoning = None
        if news is not None:
            for ev in journal.recent(["llm.reasoning"], limit=SCAN):
                r = payload_as(ev, LlmReasoning)
                if (
                    news.assessment_id in r.news_assessment_ids
                    and set(r.prediction_ids) <= allowed_forecasts
                    and live(r.available_at, r.valid_until, now)
                ):
                    reasoning = r
                    break
        return Evidence(forecast=forecast, news=[news] if news else [], reasoning=reasoning)

    return evidence
