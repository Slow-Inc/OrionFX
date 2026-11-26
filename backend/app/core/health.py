"""Component heartbeats (PRD-08 FR-009). Stale or unseen reads as DOWN."""

from collections.abc import Callable
from datetime import datetime, timedelta

from app.core.contracts import HealthUpdated
from app.core.enums import HealthStatus
from app.core.time import utc_now


def effective_health(beat: HealthUpdated, now: datetime, ttl: timedelta) -> HealthUpdated:
    """A heartbeat older than `ttl` reads as DOWN ("stale"), whoever reports it."""
    if now - beat.last_heartbeat > ttl:
        return beat.model_copy(update={"status": HealthStatus.DOWN, "detail": "stale"})
    return beat


class HealthRegistry:
    def __init__(
        self, *, clock: Callable[[], datetime] = utc_now, ttl: timedelta = timedelta(seconds=30)
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._beats: dict[str, HealthUpdated] = {}

    def beat(self, component: str, status: HealthStatus, detail: str | None = None) -> None:
        self._beats[component] = HealthUpdated(
            component=component, status=status, last_heartbeat=self._clock(), detail=detail
        )

    def _effective(self, beat: HealthUpdated) -> HealthUpdated:
        return effective_health(beat, self._clock(), self._ttl)

    def status(self, component: str) -> HealthStatus:
        beat = self._beats.get(component)
        return HealthStatus.DOWN if beat is None else self._effective(beat).status

    def snapshot(self) -> dict[str, HealthUpdated]:
        return {name: self._effective(beat) for name, beat in self._beats.items()}
