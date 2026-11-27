"""Shared fixtures for the integration tests: a migrated database, the fake broker and
a clock driven by it."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.fake_broker import FakeBroker
from app.storage.db import connect, migrate

D = Decimal
T0 = datetime(2026, 1, 5, 10, tzinfo=UTC)


class Clock:
    def __init__(self, broker: FakeBroker) -> None:
        self.broker = broker

    def __call__(self) -> datetime:
        return self.broker.now

    def advance(self, seconds: float) -> None:
        self.broker.now += timedelta(seconds=seconds)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "engine.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def broker() -> FakeBroker:
    b = FakeBroker(balance=D("1000"), now=T0)
    b.set_quote("XAUUSD", bid=D("2000.00"), ask=D("2000.00"))
    return b


@pytest.fixture
def clock(broker: FakeBroker) -> Clock:
    return Clock(broker)
