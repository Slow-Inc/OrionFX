"""M08 slice A: config, secrets, health, logging (PRD-08 FR-004..006, 009, 010)."""

import io
import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import ProtectedPolicy, UserConfig, missing_for_start
from app.core.enums import HealthStatus
from app.core.health import HealthRegistry
from app.core.logging import JsonFormatter
from app.core.secrets import redact, resolve_secret
from app.storage.config_store import ConfigStore, VersionConflict
from app.storage.db import connect, migrate


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "db.sqlite")
    migrate(c)
    yield c
    c.close()


def test_default_config_is_not_ready_to_trade() -> None:
    cfg = UserConfig()
    assert set(missing_for_start(cfg)) == {
        "trading.initial_lot",
        "trading.profit_target_usd",
        "trading.max_drawdown_percent",
    }
    assert ProtectedPolicy().live_allowed is False


def test_complete_config_has_nothing_missing() -> None:
    cfg = UserConfig.model_validate(
        {
            "trading": {
                "initial_lot": "0.01",
                "profit_target_usd": "10",
                "max_drawdown_percent": "20",
            }
        }
    )
    assert missing_for_start(cfg) == []
    assert cfg.trading.profit_target_usd == Decimal("10")


def test_plaintext_secret_rejected() -> None:
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"llm": {"api_key_ref": "sk-live-abc123"}})
    UserConfig.model_validate({"llm": {"api_key_ref": "env:ORIONFX_LLM_KEY"}})


def test_protected_fields_not_user_editable() -> None:
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"system": {"live_allowed": True}})
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"trading": {"account_mode": "LIVE"}})
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"trading": {"order_scope": "ALL_ORDERS"}})


def test_config_version_conflict(conn: sqlite3.Connection) -> None:
    store = ConfigStore(conn)
    v0, cfg = store.current()
    assert v0 == 0
    v1 = store.update(cfg, expected_version=0, actor="owner")
    assert v1 == 1
    with pytest.raises(VersionConflict):
        store.update(cfg, expected_version=0, actor="owner")
    assert store.current()[0] == 1


def test_resolve_secret_reads_environment() -> None:
    assert resolve_secret("env:X_KEY", env={"X_KEY": "s3cr3t"}) == "s3cr3t"
    assert resolve_secret("env:MISSING", env={}) is None
    with pytest.raises(ValueError):
        resolve_secret("plain", env={})


def test_redact_masks_secret_keys_and_values() -> None:
    data = {"api_key": "abc", "nested": {"Authorization": "Bearer t"}, "note": "uses s3cr3t"}
    out = redact(data, secrets=["s3cr3t"])
    assert out == {
        "api_key": "***",
        "nested": {"Authorization": "***"},
        "note": "uses ***",
    }


def test_log_redaction() -> None:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter(secrets=["topsecret"]))
    log = logging.getLogger("orionfx.test")
    log.handlers = [handler]
    log.propagate = False
    log.warning("calling provider with topsecret", extra={"fields": {"password": "pw"}})
    record = json.loads(buf.getvalue())
    assert "topsecret" not in buf.getvalue() and "pw" not in record["fields"].values()
    assert record["level"] == "WARNING"


def test_health_goes_stale_after_ttl() -> None:
    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    reg = HealthRegistry(clock=lambda: now[0], ttl=timedelta(seconds=10))
    reg.beat("M02", HealthStatus.OK)
    assert reg.status("M02") is HealthStatus.OK
    now[0] += timedelta(seconds=11)
    assert reg.status("M02") is HealthStatus.DOWN
    assert reg.status("never-seen") is HealthStatus.DOWN
    assert reg.snapshot()["M02"].status is HealthStatus.DOWN


def test_redact_masks_whole_values_under_secret_keys() -> None:
    out = redact(
        {
            "api_keys": ["sk-1"],
            "authorization": {"scheme": "Bearer", "credentials": "tok"},
            "password": ("pw",),
            "idempotency_id": "cmd-42",
            "monkey": "banana",
        }
    )
    assert out["api_keys"] == "***"
    assert out["authorization"] == "***"
    assert out["password"] == "***"
    assert out["monkey"] == "banana"


def test_redact_scrubs_models_and_unknown_objects() -> None:
    from app.core.config import LlmConfig

    out = redact({"cfg": LlmConfig(api_key_ref="env:K"), "x": object()}, secrets=["env:K"])
    assert out["cfg"]["api_key_ref"] == "***"
