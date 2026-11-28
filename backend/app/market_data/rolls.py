"""Futures contract roll dates per candle dataset version (#128, PRD #120 slice 1).

The FirstRate GC series chains front-month contracts and rolls "on a fixed date for each
ticker, set from that ticker's historical volume changeover", at midnight Eastern (for a
Monday roll, the Sunday evening session). The imported series is ratio-adjusted, which
leaves scale-free features unchanged, so rows that cross a roll are kept. The roll dates
are still recorded, append-only, so the small residual FirstRate leaves at each join can
be checked and an exclusion arm can find the joins.

FirstRate's own listing of the contracts used (its 'Other Data' tab) is not public, so its
format is unknown. Convert it to this CSV, version 1:
- UTF-8, comma-separated, header `roll_at,from_contract,to_contract`;
- `roll_at`: ISO 8601 with a UTC offset, the instant the new contract's data starts
  (`2026-01-26T00:00:00-05:00`);
- `from_contract`, `to_contract`: exchange codes such as `GCG26`, different from each other.
"""

import csv
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.time import utc_iso
from app.market_data.history import open_text
from app.market_data.sources import SourceRegistry
from app.storage.db import transaction

__all__ = ["Roll", "RollConflict", "RollLog", "read_rolls"]

HEADER = ("roll_at", "from_contract", "to_contract")


class RollConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Roll:
    roll_at: datetime
    from_contract: str
    to_contract: str

    def __post_init__(self) -> None:
        if self.roll_at.tzinfo is None:
            raise ValueError("roll_at needs a UTC offset")
        object.__setattr__(self, "roll_at", self.roll_at.astimezone(UTC))
        if not self.from_contract or not self.to_contract:
            raise ValueError("a roll names both contracts")
        if self.from_contract == self.to_contract:
            raise ValueError("a roll changes the contract")


class RollLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, dataset_version: str, rolls: Iterable[Roll]) -> None:
        """Record rolls for a version that has a data source; a roll already recorded
        identically is a no-op, a different one at the same instant is refused."""
        SourceRegistry(self._conn).require(dataset_version)
        with transaction(self._conn):
            for r in rolls:
                at = utc_iso(r.roll_at)
                self._conn.execute(
                    "INSERT INTO candle_rolls (dataset_version, roll_at, from_contract,"
                    " to_contract) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (dataset_version, at, r.from_contract, r.to_contract),
                )
                stored = self._conn.execute(
                    "SELECT from_contract, to_contract FROM candle_rolls"
                    " WHERE dataset_version = ? AND roll_at = ?",
                    (dataset_version, at),
                ).fetchone()
                if tuple(stored) != (r.from_contract, r.to_contract):
                    raise RollConflict(f"{dataset_version} already rolls {tuple(stored)} at {at}")

    def rolls_of(self, dataset_version: str) -> list[Roll]:
        rows = self._conn.execute(
            "SELECT roll_at, from_contract, to_contract FROM candle_rolls"
            " WHERE dataset_version = ? ORDER BY roll_at",
            (dataset_version,),
        ).fetchall()
        return [Roll(datetime.fromisoformat(t), a, b) for t, a, b in rows]


def read_rolls(path: Path) -> list[Roll]:
    """Rolls from a CSV in the format above; a malformed row refuses the whole file."""
    with open_text(path) as f:
        reader = csv.reader(f)
        if tuple(next(reader, ())) != HEADER:
            raise ValueError(f"{path}: header must be {','.join(HEADER)}")
        out = []
        for line, row in enumerate(reader, start=2):
            try:
                t, a, b = row
                out.append(Roll(datetime.fromisoformat(t), a, b))
            except ValueError as exc:
                raise ValueError(f"{path} line {line}: {exc}") from exc
        return out
