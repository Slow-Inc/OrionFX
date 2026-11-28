"""A stand-in for #132's `HoldoutOpening` (the `HoldoutToken` shape) until the run log
lands on this branch. Test support only: it opens nothing by itself."""

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Opened:
    protocol_hash: str = "sha256:" + "0" * 64
    config_id: str = "c1"
    opened_at: datetime = datetime(2026, 11, 28, tzinfo=UTC)
