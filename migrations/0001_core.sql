-- M08 core tables (PRD-08). Timestamps are ISO-8601 UTC text.

CREATE TABLE events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    event_type     TEXT NOT NULL,
    occurred_at    TEXT NOT NULL,
    recorded_at    TEXT NOT NULL,
    account_id     TEXT NOT NULL,
    bot_id         TEXT NOT NULL,
    cycle_id       TEXT,
    correlation_id TEXT NOT NULL,
    body           TEXT NOT NULL
);
CREATE INDEX events_by_type ON events (event_type, seq);
CREATE INDEX events_by_cycle ON events (cycle_id, seq);

CREATE TABLE consumer_offsets (
    consumer TEXT PRIMARY KEY,
    last_seq INTEGER NOT NULL
);

CREATE TABLE config_versions (
    version    INTEGER PRIMARY KEY,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    actor      TEXT NOT NULL
);

CREATE TABLE executor_leases (
    namespace  TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE system_flags (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE owner (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE sessions (
    token_sha256 TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
