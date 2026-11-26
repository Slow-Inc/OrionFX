"""Single-owner authentication (PRD-08 FR-011, SYS-08).

- Password: scrypt (n=2^14, r=8, p=1), 16-byte random salt, constant-time compare.
- Session: random 32-byte token; only its SHA-256 is stored, with an expiry.
- Failed logins: at most `max_failures` per `window`, then every login (even a
  correct one) is refused until the window passes.
"""

import base64
import hashlib
import hmac
import secrets
import sqlite3
from collections import deque
from datetime import datetime, timedelta

from app.core.time import Clock, utc_now
from app.storage.audit import audit
from app.storage.db import transaction

_N, _R, _P = 2**14, 8, 1
SESSION_TTL = timedelta(hours=12)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = base64.b64decode(digest_b64)
    actual = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p)
    )
    return hmac.compare_digest(actual, expected)


def set_owner_password(conn: sqlite3.Connection, password: str) -> None:
    if len(password) < 12:
        raise ValueError("owner password must be at least 12 characters")
    with transaction(conn):
        conn.execute(
            "INSERT INTO owner (id, password_hash, updated_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash,"
            " updated_at = excluded.updated_at",
            (hash_password(password), utc_now().isoformat()),
        )
        conn.execute("DELETE FROM sessions")  # a new password ends every old session
        audit(conn, actor="owner", action="auth.set_password", detail="sessions revoked")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def login(self, password: str) -> tuple[str, datetime] | None:
        row = self._conn.execute("SELECT password_hash FROM owner WHERE id = 1").fetchone()
        if row is None or not verify_password(password, row[0]):
            return None
        token = secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + SESSION_TTL
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO sessions (token_sha256, created_at, expires_at) VALUES (?, ?, ?)",
                (_token_hash(token), now.isoformat(), expires.isoformat()),
            )
            audit(self._conn, actor="owner", action="auth.login", detail="session created")
        return token, expires

    def is_valid(self, token: str) -> bool:
        row = self._conn.execute(
            "SELECT expires_at FROM sessions WHERE token_sha256 = ?", (_token_hash(token),)
        ).fetchone()
        return row is not None and datetime.fromisoformat(row[0]) > self._clock()

    def logout(self, token: str) -> None:
        with transaction(self._conn):
            self._conn.execute("DELETE FROM sessions WHERE token_sha256 = ?", (_token_hash(token),))


class LoginThrottle:
    """Process-wide: there is one owner, so one window guards every login."""

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        max_failures: int = 5,
        window: timedelta = timedelta(minutes=1),
    ) -> None:
        self._clock = clock
        self._max = max_failures
        self._window = window
        self._failures: deque[datetime] = deque()

    def _prune(self) -> None:
        cutoff = self._clock() - self._window
        while self._failures and self._failures[0] <= cutoff:
            self._failures.popleft()

    def blocked(self) -> bool:
        self._prune()
        return len(self._failures) >= self._max

    def record_failure(self) -> None:
        self._failures.append(self._clock())
