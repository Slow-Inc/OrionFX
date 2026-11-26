"""Review findings on PRD-08 slice B: throttle races, WS input/timeouts, read-model gaps."""

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.app import ApiSettings, create_app
from app.api.auth import LoginThrottle, SessionStore, set_owner_password, verify_password
from app.core.config import SystemConfig
from app.core.enums import Mode
from app.core.events import make_event
from app.core.secrets import MASK, unredact
from app.storage.db import connect, migrate
from app.storage.journal import Journal

PASSWORD = "correct horse battery staple"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "api.db"
    conn = connect(path)
    migrate(conn)
    set_owner_password(conn, PASSWORD)
    conn.close()
    return path


def _client(db: Path, clock: Clock, **settings: object) -> TestClient:
    return TestClient(create_app(ApiSettings(db_path=db, **settings), clock=clock))  # type: ignore[arg-type]


def _token(c: TestClient) -> str:
    return str(c.post("/api/auth/login", json={"password": PASSWORD}).json()["token"])


def _health(j: Journal, component: str, status: str, at: datetime) -> None:
    j.append(
        make_event(
            "health.updated",
            source=component,
            payload={"component": component, "status": status, "last_heartbeat": at},
            account_id="local-account",
            bot_id="bot-1",
            mode=Mode.DEMO,
        )
    )


def test_throttle_reserves_atomically_under_concurrency() -> None:
    clock = Clock()
    throttle = LoginThrottle(clock=clock, max_failures=5)
    admitted: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        ok = throttle.try_reserve()
        with lock:
            admitted.append(ok)

    threads = [threading.Thread(target=attempt) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert admitted.count(True) == 5


def test_successful_login_frees_its_reservation() -> None:
    throttle = LoginThrottle(clock=Clock(), max_failures=1)
    assert throttle.try_reserve()
    throttle.succeeded()
    assert throttle.try_reserve()


def test_corrupt_password_hash_fails_closed() -> None:
    assert verify_password("x", "scrypt$abc$8$1$AAAA$AAAA") is False
    assert verify_password("x", "scrypt$16384$8$1$!!notb64$AAAA") is False
    assert verify_password("x", f"scrypt${2**30}$8$1$AAAA$AAAA") is False  # absurd cost


def test_login_purges_expired_sessions(db: Path) -> None:
    clock = Clock()
    conn = connect(db)
    store = SessionStore(conn, clock=clock)
    store.login(PASSWORD)
    clock.now += timedelta(hours=13)
    store.login(PASSWORD)
    rows = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert rows == 1


def test_ws_hello_timeout_closes(db: Path) -> None:
    with (
        _client(db, Clock(), ws_hello_timeout=0.2) as c,
        c.websocket_connect("/ws/live") as ws,
        pytest.raises(WebSocketDisconnect) as exc,
    ):
        ws.receive_json()
    assert exc.value.code == 4408


@pytest.mark.parametrize("hello", [{"since": 0}, {"token": "t", "since": "x"}, {"token": 1}])
def test_ws_malformed_hello_closes_4400(db: Path, hello: dict[str, object]) -> None:
    with _client(db, Clock()) as c, c.websocket_connect("/ws/live") as ws:
        ws.send_json(hello)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4400


def test_down_component_never_falls_out_of_status(db: Path) -> None:
    clock = Clock()
    j = Journal(connect(db))
    _health(j, "mt5-bridge", "DOWN", clock.now)
    for _ in range(60):
        _health(j, "M02", "OK", clock.now)
    with _client(db, clock) as c:
        h = c.get("/api/status", headers={"Authorization": f"Bearer {_token(c)}"}).json()
    assert h["health"]["mt5-bridge"]["status"] == "DOWN"


def test_stale_heartbeat_reads_down_in_status(db: Path) -> None:
    clock = Clock()
    j = Journal(connect(db))
    _health(j, "M02", "OK", clock.now)
    with _client(db, clock) as c:
        headers = {"Authorization": f"Bearer {_token(c)}"}
        clock.now += timedelta(minutes=5)
        h = c.get("/api/status", headers=headers).json()
    assert h["health"]["M02"]["status"] == "DOWN"


def test_status_exposes_the_source_of_each_field(db: Path) -> None:
    with _client(db, Clock()) as c:
        s = c.get("/api/status", headers={"Authorization": f"Bearer {_token(c)}"}).json()
    assert "control_state_seq" in s and "risk_lock_seq" in s


def test_limit_is_validated(db: Path) -> None:
    with _client(db, Clock()) as c:
        headers = {"Authorization": f"Bearer {_token(c)}"}
        assert c.get("/api/cycles?limit=0", headers=headers).status_code == 422
        assert c.get("/api/cycles?limit=100000", headers=headers).status_code == 422


def test_unredact_restores_any_masked_secret_field() -> None:
    current = {"llm": {"api_key_ref": "env:A"}, "x": {"token": "env:B"}}
    incoming = {"llm": {"api_key_ref": MASK}, "x": {"token": MASK}, "y": 1}
    assert unredact(incoming, current) == {
        "llm": {"api_key_ref": "env:A"},
        "x": {"token": "env:B"},
        "y": 1,
    }


def test_api_defaults_come_from_system_config() -> None:
    s = ApiSettings(db_path=Path("x"))
    assert s.bind_host == SystemConfig().bind_host
    assert s.dashboard_origin == SystemConfig().dashboard_origin


def test_sqlite3_import_used() -> None:  # keeps the import honest for type-checkers
    assert sqlite3.sqlite_version
