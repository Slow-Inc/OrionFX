"""Shared fixtures for the integration tests: a migrated database, the fake broker and
a clock driven by it."""

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.engine import T0, Clock

from app.execution.fake_broker import FakeBroker
from app.storage.db import connect, migrate

D = Decimal


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
