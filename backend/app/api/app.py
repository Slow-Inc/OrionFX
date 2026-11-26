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
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ValidationError
from starlette.websockets import WebSocketDisconnect

from app.api.auth import LoginThrottle, SessionStore
from app.core.config import ProtectedPolicy, UserConfig, missing_for_start
from app.core.contracts import ControlRequested
from app.core.enums import CommandStatus, ControlCommand, ControlState, Mode
from app.core.events import make_event
from app.core.ids import new_id
from app.core.secrets import MASK, redact
from app.core.time import Clock, utc_now
from app.storage.audit import audit
from app.storage.config_store import ConfigStore, VersionConflict
from app.storage.db import connect, transaction
from app.storage.flags import REQUIRES_RECONCILE, get_bool_flag
from app.storage.journal import Journal

ACCOUNT_STALE_AFTER = timedelta(seconds=60)
WS_POLL_SECONDS = 0.5

COMMANDS: dict[str, ControlCommand] = {
    "start": ControlCommand.START,
    "pause": ControlCommand.PAUSE,
    "finish-cycle": ControlCommand.FINISH_CYCLE,
    "emergency-stop": ControlCommand.EMERGENCY_STOP,
    "approve-recovery": ControlCommand.APPROVE_RECOVERY,
}


@dataclass(frozen=True)
class ApiSettings:
    db_path: Path
    bind_host: str = "127.0.0.1"
    port: int = 8000
    dashboard_origin: str = "http://127.0.0.1:5173"
    account_id: str = "local-account"
    bot_id: str = "bot-1"


class LoginBody(BaseModel):
    password: str


class SettingsPatch(BaseModel):
    expected_version: int
    config: dict[str, Any]


def create_app(settings: ApiSettings, *, clock: Clock = utc_now) -> FastAPI:
    app = FastAPI(title="OrionFX", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.dashboard_origin],
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
        if throttle.blocked():
            raise HTTPException(status_code=429, detail="too many failed logins; wait")
        result = SessionStore(conn, clock=clock).login(body.password)
        if result is None:
            throttle.record_failure()
            raise HTTPException(status_code=401, detail="invalid password")
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
        completed = journal.recent(["control.completed"], limit=1)
        risk = journal.recent(["risk.updated"], limit=1)
        control_state = ControlState.STOPPED
        risk_locked = False
        if completed:
            payload = completed[0].envelope.payload
            control_state = payload.resulting_state  # type: ignore[attr-defined]
            risk_locked = payload.risk_locked  # type: ignore[attr-defined]
        if risk:
            risk_locked = risk_locked or risk[0].envelope.payload.risk_locked  # type: ignore[attr-defined]
        health = {
            e.envelope.payload.component: e.envelope.payload.model_dump(mode="json")  # type: ignore[attr-defined]
            for e in reversed(journal.recent(["health.updated"], limit=50))
        }
        return {
            "as_of": clock().isoformat(),
            "control_state": control_state,
            "risk_locked": risk_locked,
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
        snap = latest[0].envelope.payload
        observed = snap.observed_at  # type: ignore[attr-defined]
        return {
            "as_of": clock().isoformat(),
            "snapshot": snap.model_dump(mode="json"),
            "stale": clock() - observed > ACCOUNT_STALE_AFTER,
        }

    @private.get("/api/positions")
    def positions() -> dict[str, Any]:
        # The authoritative position read model belongs to M06 (#4 plan order).
        return {"as_of": clock().isoformat(), "available": False, "items": []}

    @private.get("/api/cycles")
    def cycles(conn: Db, limit: int = 50) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for e in Journal(conn).recent(["cycle.updated"], limit=max(1, min(limit, 500)) * 10):
            key = e.envelope.cycle_id or "unknown"
            latest.setdefault(key, {"cycle_id": key, **e.envelope.payload.model_dump(mode="json")})
        return {"as_of": clock().isoformat(), "items": list(latest.values())[:limit]}

    @private.get("/api/reports")
    def reports() -> dict[str, Any]:
        return {"as_of": clock().isoformat(), "available": False, "items": []}

    def _recent_payloads(conn: sqlite3.Connection, event_type: str, limit: int) -> dict[str, Any]:
        items = Journal(conn).recent([event_type], limit=max(1, min(limit, 200)))
        return {
            "as_of": clock().isoformat(),
            "items": [{"seq": e.seq, **e.envelope.payload.model_dump(mode="json")} for e in items],
        }

    @private.get("/api/predictions")
    def predictions(conn: Db, limit: int = 50) -> dict[str, Any]:
        return _recent_payloads(conn, "prediction.generated", limit)

    @private.get("/api/decisions")
    def decisions(conn: Db, limit: int = 50) -> dict[str, Any]:
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
        raw = body.config
        # The dashboard echoes the masked value back; "***" means "keep what is stored".
        llm = raw.get("llm")
        if isinstance(llm, dict) and llm.get("api_key_ref") == MASK:
            raw = {**raw, "llm": {**llm, "api_key_ref": current.llm.api_key_ref}}
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

    @app.websocket("/ws/live")
    async def live(ws: WebSocket) -> None:
        """First message: {"token": ..., "since": <seq>}. Then events with seq > since."""
        await ws.accept()
        conn = connect(settings.db_path)
        try:
            hello = await ws.receive_json()
            token = str(hello.get("token", "")) if isinstance(hello, dict) else ""
            sessions = SessionStore(conn, clock=clock)
            if not sessions.is_valid(token):
                await ws.close(code=4401)
                return
            cursor = int(hello.get("since", 0))
            journal = Journal(conn)
            while True:
                if not sessions.is_valid(token):
                    await ws.close(code=4401)
                    return
                for seq, body in journal.read_raw_since(cursor):
                    await ws.send_json({"seq": seq, "event": json.loads(body)})
                    cursor = seq
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=WS_POLL_SECONDS)
                except TimeoutError:
                    continue
                if isinstance(msg, dict) and "since" in msg:  # client-requested resync
                    cursor = int(msg["since"])
        except WebSocketDisconnect:
            return
        finally:
            conn.close()

    @app.exception_handler(sqlite3.Error)
    async def db_error(_: Request, exc: sqlite3.Error) -> Response:
        return Response(status_code=503, content="storage unavailable")

    return app
