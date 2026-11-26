"""Local owner API (PRD-08 slice B, master plan §8.4).

Every route except login and a content-free liveness probe requires a session.
A control command is *recorded* (`control.requested` + audit) and answered with
202 and a command id; it never claims a fill or a close. There is no route that
enables Live.
"""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.websockets import WebSocketDisconnect

from app.api.auth import LoginThrottle, SessionStore
from app.core.config import ProtectedPolicy, SystemConfig, UserConfig, missing_for_start
from app.core.contracts import (
    AccountSnapshot,
    ControlCompleted,
    ControlRequested,
    HealthUpdated,
    RiskUpdated,
)
from app.core.enums import CommandStatus, ControlCommand, ControlState, Mode
from app.core.events import make_event
from app.core.health import effective_health
from app.core.ids import new_id
from app.core.secrets import redact, unredact
from app.core.time import Clock, utc_now
from app.storage.audit import audit
from app.storage.config_store import ConfigStore, VersionConflict
from app.storage.db import connect, transaction
from app.storage.flags import REQUIRES_RECONCILE, get_bool_flag
from app.storage.journal import Journal, payload_as

ACCOUNT_STALE_AFTER = timedelta(seconds=60)
HEALTH_TTL = timedelta(seconds=60)
WS_POLL_SECONDS = 0.5

# "emergency-stop" <-> ControlCommand.EMERGENCY_STOP, derived from the enum.
COMMANDS: dict[str, ControlCommand] = {c.value.lower().replace("_", "-"): c for c in ControlCommand}

Limit = Annotated[int, Query(ge=1, le=200)]
_SYSTEM_DEFAULTS = SystemConfig()


@dataclass(frozen=True)
class ApiSettings:
    db_path: Path
    bind_host: str = _SYSTEM_DEFAULTS.bind_host
    port: int = 8000
    dashboard_origin: str = _SYSTEM_DEFAULTS.dashboard_origin
    account_id: str = "local-account"
    bot_id: str = "bot-1"
    ws_hello_timeout: float = 5.0
    extra_origins: tuple[str, ...] = field(default_factory=tuple)


class LoginBody(BaseModel):
    password: str


class SettingsPatch(BaseModel):
    expected_version: int
    config: dict[str, Any]


class WsHello(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str
    since: int = Field(default=0, ge=0)


class WsResync(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    since: int = Field(ge=0)


def create_app(settings: ApiSettings, *, clock: Clock = utc_now) -> FastAPI:
    app = FastAPI(title="OrionFX", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.dashboard_origin, *settings.extra_origins],
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )
    throttle = LoginThrottle(clock=clock)
    bearer = HTTPBearer(auto_error=False)

    def db() -> Iterator[sqlite3.Connection]:
        # One connection per request: a sqlite3 connection must not be shared by
        # concurrent threads (see storage.db.transaction).
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    Db = Annotated[sqlite3.Connection, Depends(db)]

    def session(
        conn: Db,
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if creds is None or not SessionStore(conn, clock=clock).is_valid(creds.credentials):
            raise HTTPException(status_code=401, detail="authentication required")
        return creds.credentials

    # --- public ------------------------------------------------------------------

    @app.get("/api/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(body: LoginBody, conn: Db) -> dict[str, str]:
        if not throttle.try_reserve():
            raise HTTPException(status_code=429, detail="too many failed logins; wait")
        result = SessionStore(conn, clock=clock).login(body.password)
        if result is None:
            raise HTTPException(status_code=401, detail="invalid password")
        throttle.succeeded()
        token, expires = result
        return {"token": token, "expires_at": expires.isoformat()}

    # --- private -----------------------------------------------------------------

    private = APIRouter(dependencies=[Depends(session)])

    @private.post("/api/auth/logout", status_code=204)
    def logout(conn: Db, token: Annotated[str, Depends(session)]) -> Response:
        SessionStore(conn, clock=clock).logout(token)
        return Response(status_code=204)

    @private.get("/api/status")
    def status(conn: Db) -> dict[str, Any]:
        journal = Journal(conn)
        version, cfg = ConfigStore(conn).current()
        now = clock()

        completed = journal.recent(["control.completed"], limit=1)
        control_state = ControlState.STOPPED
        control_seq = None
        if completed:
            control_state = payload_as(completed[0], ControlCompleted).resulting_state
            control_seq = completed[0].seq

        # Both events carry the lock; the newer one is M06's current word on it.
        # Each field reports the seq it came from, so a dashboard can tell when
        # the lock is newer than the last control transition.
        lock_events = journal.recent(["control.completed", "risk.updated"], limit=1)
        risk_locked = False
        lock_seq = None
        if lock_events:
            ev = lock_events[0]
            if ev.envelope.event_type == "control.completed":
                risk_locked = payload_as(ev, ControlCompleted).risk_locked
            else:
                risk_locked = payload_as(ev, RiskUpdated).risk_locked
            lock_seq = ev.seq

        health = {}
        for ev in journal.latest_per_payload_field("health.updated", "component"):
            beat = effective_health(payload_as(ev, HealthUpdated), now, HEALTH_TTL)
            health[beat.component] = beat.model_dump(mode="json")

        return {
            "as_of": now.isoformat(),
            "control_state": control_state,
            "control_state_seq": control_seq,
            "risk_locked": risk_locked,
            "risk_lock_seq": lock_seq,
            "requires_reconcile": get_bool_flag(conn, REQUIRES_RECONCILE),
            "config_version": version,
            "account_mode": cfg.trading.account_mode,
            "missing_for_start": missing_for_start(cfg),
            "live_allowed": ProtectedPolicy().live_allowed,
            "health": health,
            "latest_seq": journal.latest_seq(),
        }

    @private.get("/api/account")
    def account(conn: Db) -> dict[str, Any]:
        latest = Journal(conn).recent(["account.snapshot"], limit=1)
        if not latest:
            return {"as_of": clock().isoformat(), "snapshot": None, "stale": True}
        snap = payload_as(latest[0], AccountSnapshot)
        return {
            "as_of": clock().isoformat(),
            "snapshot": snap.model_dump(mode="json"),
            "stale": clock() - snap.observed_at > ACCOUNT_STALE_AFTER,
        }

    @private.get("/api/positions")
    def positions() -> dict[str, Any]:
        # The authoritative position read model belongs to M06 (#4 plan order).
        return {"as_of": clock().isoformat(), "available": False, "items": []}

    @private.get("/api/cycles")
    def cycles(conn: Db, limit: Limit = 50) -> dict[str, Any]:
        items = [
            {
                "cycle_id": e.envelope.cycle_id,
                "seq": e.seq,
                **e.envelope.payload.model_dump(mode="json"),
            }
            for e in Journal(conn).latest_per_cycle("cycle.updated", limit=limit)
        ]
        return {"as_of": clock().isoformat(), "items": items}

    @private.get("/api/reports")
    def reports() -> dict[str, Any]:
        return {"as_of": clock().isoformat(), "available": False, "items": []}

    def _recent_payloads(conn: sqlite3.Connection, event_type: str, limit: int) -> dict[str, Any]:
        items = Journal(conn).recent([event_type], limit=limit)
        return {
            "as_of": clock().isoformat(),
            "items": [{"seq": e.seq, **e.envelope.payload.model_dump(mode="json")} for e in items],
        }

    @private.get("/api/predictions")
    def predictions(conn: Db, limit: Limit = 50) -> dict[str, Any]:
        return _recent_payloads(conn, "prediction.generated", limit)

    @private.get("/api/decisions")
    def decisions(conn: Db, limit: Limit = 50) -> dict[str, Any]:
        return _recent_payloads(conn, "decision.created", limit)

    @private.get("/api/settings")
    def get_settings(conn: Db) -> dict[str, Any]:
        version, cfg = ConfigStore(conn).current()
        return {
            "version": version,
            "config": redact(cfg.model_dump(mode="json")),
            "protected": ProtectedPolicy().model_dump(mode="json"),
            "missing_for_start": missing_for_start(cfg),
        }

    @private.patch("/api/settings")
    def patch_settings(body: SettingsPatch, conn: Db) -> dict[str, int]:
        store = ConfigStore(conn)
        _, current = store.current()
        # The dashboard echoes masked values back; MASK means "keep what is stored".
        raw = unredact(body.config, current.model_dump(mode="json"))
        try:
            cfg = UserConfig.model_validate(raw)
        except ValidationError as exc:
            errors = json.loads(exc.json(include_input=False, include_url=False))
            raise HTTPException(status_code=422, detail=errors) from None
        try:
            version = store.update(cfg, expected_version=body.expected_version, actor="owner")
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"version": version}

    @private.post("/api/bot/{action}", status_code=202)
    def control(action: str, conn: Db) -> dict[str, str]:
        command = COMMANDS.get(action)
        if command is None:
            raise HTTPException(status_code=404, detail="unknown command")
        _, cfg = ConfigStore(conn).current()
        if command is ControlCommand.START and (missing := missing_for_start(cfg)):
            raise HTTPException(status_code=409, detail={"missing": missing})
        command_id = new_id("cmd")
        env = make_event(
            "control.requested",
            source="M08",
            payload=ControlRequested(command_id=command_id, command=command, actor="owner"),
            account_id=settings.account_id,
            bot_id=settings.bot_id,
            mode=Mode(cfg.trading.account_mode),
        )
        with transaction(conn):
            Journal(conn).append(env)
            audit(conn, actor="owner", action=f"control.{command}", detail=command_id)
        return {"command_id": command_id, "status": CommandStatus.ACCEPTED}

    @private.get("/api/commands/{command_id}")
    def command_status(command_id: str, conn: Db) -> dict[str, Any]:
        journal = Journal(conn)
        if journal.find("control.requested", "command_id", command_id) is None:
            raise HTTPException(status_code=404, detail="unknown command")
        done = journal.find("control.completed", "command_id", command_id)
        if done is None:
            return {"command_id": command_id, "status": CommandStatus.ACCEPTED, "unresolved": []}
        return done.envelope.payload.model_dump(mode="json")

    app.include_router(private)

    # --- live feed ---------------------------------------------------------------

    def _session_valid(token: str) -> bool:
        conn = connect(settings.db_path)
        try:
            return SessionStore(conn, clock=clock).is_valid(token)
        finally:
            conn.close()

    def _events_since(cursor: int) -> list[tuple[int, str]]:
        conn = connect(settings.db_path)
        try:
            return Journal(conn).read_raw_since(cursor)
        finally:
            conn.close()

    @app.websocket("/ws/live")
    async def live(ws: WebSocket) -> None:
        """First message: {"token": ..., "since": <seq>} within the hello timeout.
        Then the server streams {"seq", "event"} for every seq > since. A later
        {"since": n} message resyncs. Close codes: 4400 bad input, 4401 auth,
        4408 hello timeout. SQLite work runs off the event loop."""
        await ws.accept()
        try:
            try:
                raw = await asyncio.wait_for(ws.receive_json(), timeout=settings.ws_hello_timeout)
            except TimeoutError:
                await ws.close(code=4408)
                return
            try:
                hello = WsHello.model_validate(raw)
            except ValidationError:
                await ws.close(code=4400)
                return
            if not await asyncio.to_thread(_session_valid, hello.token):
                await ws.close(code=4401)
                return
            cursor = hello.since
            while True:
                if not await asyncio.to_thread(_session_valid, hello.token):
                    await ws.close(code=4401)
                    return
                for seq, body in await asyncio.to_thread(_events_since, cursor):
                    await ws.send_text(f'{{"seq":{seq},"event":{body}}}')
                    cursor = seq
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=WS_POLL_SECONDS)
                except TimeoutError:
                    continue
                try:
                    cursor = WsResync.model_validate(msg).since
                except ValidationError:
                    await ws.close(code=4400)
                    return
        except (WebSocketDisconnect, json.JSONDecodeError):
            return

    @app.exception_handler(sqlite3.Error)
    async def db_error(_: Request, exc: sqlite3.Error) -> Response:
        return Response(status_code=503, content="storage unavailable")

    return app
