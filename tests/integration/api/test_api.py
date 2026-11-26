"""M08 slice B: API, owner auth, control commands, WebSocket (PRD-08 FR-011..015, AT-20, AT-21)."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.app import ApiSettings, create_app
from app.api.auth import hash_password, set_owner_password, verify_password
from app.core.enums import Mode
from app.core.events import make_event
from app.storage.db import connect, migrate
from app.storage.journal import Journal

PASSWORD = "correct horse battery staple"
PUBLIC = {"/api/auth/login", "/api/healthz"}


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


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(db: Path, clock: Clock) -> Iterator[TestClient]:
    app = create_app(ApiSettings(db_path=db), clock=clock)
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _configure(client: TestClient, headers: dict[str, str]) -> None:
    current = client.get("/api/settings", headers=headers).json()
    cfg = current["config"]
    cfg["trading"].update(
        {"initial_lot": "0.01", "profit_target_usd": "10", "max_drawdown_percent": "20"}
    )
    r = client.patch(
        "/api/settings",
        json={"expected_version": current["version"], "config": cfg},
        headers=headers,
    )
    assert r.status_code == 200, r.text


# --- auth ----------------------------------------------------------------------


def test_password_hash_round_trip_and_salted() -> None:
    a, b = hash_password("pw"), hash_password("pw")
    assert a != b
    assert verify_password("pw", a) and not verify_password("nope", a)


def test_every_private_route_requires_session(client: TestClient, db: Path) -> None:
    # Derived from the app's own route table, so a route added later is covered.
    def flatten(routes: list[object]) -> list[object]:
        out: list[object] = []
        for r in routes:
            inner = getattr(r, "original_router", None)  # included routers (FastAPI >= 0.120)
            out.extend(flatten(inner.routes) if inner is not None else [r])
        return out

    routes = flatten(list(create_app(ApiSettings(db_path=db)).routes))
    private = [r for r in routes if isinstance(r, APIRoute) and r.path not in PUBLIC]
    assert len(private) >= 10, f"route discovery found only {len(private)}"
    for route in private:
        for method in route.methods:
            path = route.path.replace("{command_id}", "cmd-x").replace("{action}", "start")
            r = client.request(method, path, json={})
            assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


def test_login_wrong_password_rejected(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401


def test_session_expires(client: TestClient, clock: Clock) -> None:
    headers = _login(client)
    assert client.get("/api/status", headers=headers).status_code == 200
    clock.now += timedelta(hours=13)
    assert client.get("/api/status", headers=headers).status_code == 401


def test_logout_revokes(client: TestClient) -> None:
    headers = _login(client)
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/status", headers=headers).status_code == 401


def test_session_token_not_stored_in_plaintext(client: TestClient, db: Path) -> None:
    token = _login(client)["Authorization"].removeprefix("Bearer ")
    conn = sqlite3.connect(db)
    dump = "\n".join(conn.iterdump())
    assert token not in dump


def test_login_rate_limited(client: TestClient) -> None:
    codes = [client.post("/api/auth/login", json={"password": "x"}).status_code for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    # even the right password is refused while the window is closed
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


# --- settings ------------------------------------------------------------------


def test_settings_redacted(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORIONFX_LLM_KEY", "sk-very-secret")
    headers = _login(client)
    current = client.get("/api/settings", headers=headers).json()
    cfg = current["config"]
    cfg["llm"]["api_key_ref"] = "env:ORIONFX_LLM_KEY"
    r = client.patch(
        "/api/settings",
        json={"expected_version": current["version"], "config": cfg},
        headers=headers,
    )
    assert r.status_code == 200
    body = client.get("/api/settings", headers=headers).text
    assert "sk-very-secret" not in body and "ORIONFX_LLM_KEY" not in body
    assert '"live_allowed":false' in body.replace(" ", "")


def test_settings_version_conflict(client: TestClient) -> None:
    headers = _login(client)
    cfg = client.get("/api/settings", headers=headers).json()["config"]
    ok = client.patch("/api/settings", json={"expected_version": 0, "config": cfg}, headers=headers)
    assert ok.status_code == 200
    stale = client.patch(
        "/api/settings", json={"expected_version": 0, "config": cfg}, headers=headers
    )
    assert stale.status_code == 409


def test_settings_cannot_enable_live(client: TestClient) -> None:
    headers = _login(client)
    cfg = client.get("/api/settings", headers=headers).json()["config"]
    cfg["trading"]["account_mode"] = "LIVE"
    r = client.patch("/api/settings", json={"expected_version": 0, "config": cfg}, headers=headers)
    assert r.status_code == 422
    cfg["trading"]["account_mode"] = "DEMO"
    cfg["system"]["live_allowed"] = True
    r = client.patch("/api/settings", json={"expected_version": 0, "config": cfg}, headers=headers)
    assert r.status_code == 422


# --- control -------------------------------------------------------------------


def test_start_refused_until_configured(client: TestClient) -> None:
    headers = _login(client)
    r = client.post("/api/bot/start", headers=headers)
    assert r.status_code == 409
    assert "trading.initial_lot" in r.json()["detail"]["missing"]


def test_control_command_is_recorded_not_executed(client: TestClient, db: Path) -> None:
    headers = _login(client)
    _configure(client, headers)
    r = client.post("/api/bot/start", headers=headers)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "ACCEPTED" and body["command_id"].startswith("cmd-")

    conn = connect(db)
    events = Journal(conn).read_since(0, event_types=["control.requested"])
    assert [e.envelope.payload.command for e in events] == ["START"]  # type: ignore[attr-defined]
    actions = [row[0] for row in conn.execute("SELECT action FROM audit_log")]
    assert "control.START" in actions


@pytest.mark.parametrize("path", ["pause", "finish-cycle", "emergency-stop", "approve-recovery"])
def test_other_commands_accepted(client: TestClient, path: str) -> None:
    headers = _login(client)
    assert client.post(f"/api/bot/{path}", headers=headers).status_code == 202


def test_command_status_follows_completion(client: TestClient, db: Path) -> None:
    headers = _login(client)
    cmd = client.post("/api/bot/emergency-stop", headers=headers).json()["command_id"]
    assert client.get(f"/api/commands/{cmd}", headers=headers).json()["status"] == "ACCEPTED"
    conn = connect(db)
    Journal(conn).append(
        make_event(
            "control.completed",
            source="M06",
            payload={
                "command_id": cmd,
                "status": "UNRESOLVED",
                "resulting_state": "CLOSING",
                "risk_locked": False,
                "unresolved": ["position 42 close rejected: market closed"],
            },
            account_id="local-account",
            bot_id="bot-1",
            mode=Mode.DEMO,
        )
    )
    got = client.get(f"/api/commands/{cmd}", headers=headers).json()
    assert got["status"] == "UNRESOLVED"
    assert got["unresolved"] == ["position 42 close rejected: market closed"]


def test_unknown_command_404(client: TestClient) -> None:
    headers = _login(client)
    assert client.get("/api/commands/cmd-nope", headers=headers).status_code == 404


# --- read models ---------------------------------------------------------------


def test_status_reports_not_ready_and_stale_honestly(client: TestClient) -> None:
    headers = _login(client)
    s = client.get("/api/status", headers=headers).json()
    assert s["control_state"] == "STOPPED"
    assert s["missing_for_start"]
    assert "as_of" in s
    acct = client.get("/api/account", headers=headers).json()
    assert acct["snapshot"] is None and acct["stale"] is True


# --- websocket -----------------------------------------------------------------


def test_ws_rejects_bad_token(client: TestClient) -> None:
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"token": "nope", "since": 0})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_streams_from_cursor(client: TestClient) -> None:
    headers = _login(client)
    for _ in range(3):
        client.post("/api/bot/pause", headers=headers)
    token = headers["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect("/ws/live") as ws:
        ws.send_json({"token": token, "since": 1})
        first = ws.receive_json()
        second = ws.receive_json()
    assert [first["seq"], second["seq"]] == [2, 3]
    assert first["event"]["event_type"] == "control.requested"


def test_healthz_is_public_and_says_nothing(client: TestClient) -> None:
    r = client.get("/api/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_default_bind_is_localhost() -> None:
    assert ApiSettings(db_path=Path("x")).bind_host == "127.0.0.1"


def test_status_risk_lock_follows_newest_event(client: TestClient, db: Path) -> None:
    headers = _login(client)
    j = Journal(connect(db))
    common = {"account_id": "local-account", "bot_id": "bot-1", "mode": Mode.DEMO}
    j.append(
        make_event(
            "control.completed",
            source="M06",
            payload={
                "command_id": "cmd-1",
                "status": "COMPLETED",
                "resulting_state": "RISK_LOCKED",
                "risk_locked": True,
            },
            **common,  # type: ignore[arg-type]
        )
    )
    assert client.get("/api/status", headers=headers).json()["risk_locked"] is True
    j.append(
        make_event(
            "risk.updated",
            source="M06",
            payload={
                "epoch_dd_percent": "0",
                "lifetime_dd_percent": "0",
                "epoch_ceiling_percent": "20",
                "lifetime_ceiling_percent": "30",
                "risk_locked": False,
                "reason_codes": ["RECOVERY_APPROVED"],
                "policy_version": "p",
            },
            **common,  # type: ignore[arg-type]
        )
    )
    assert client.get("/api/status", headers=headers).json()["risk_locked"] is False
