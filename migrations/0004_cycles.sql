-- M06 cycle book (PRD-06 #15). One row per Cycle; money values are Decimal text.
CREATE TABLE cycles (
    cycle_id         TEXT PRIMARY KEY,
    login            TEXT NOT NULL,
    server           TEXT NOT NULL,
    state            TEXT NOT NULL,
    target_usd       TEXT NOT NULL,
    config_version   INTEGER NOT NULL,
    last_net         TEXT,
    peak_net         TEXT,
    trailing_active  INTEGER NOT NULL DEFAULT 0 CHECK (trailing_active IN (0, 1)),
    trailing_floor   TEXT,
    close_reason     TEXT,
    target_triggered INTEGER NOT NULL DEFAULT 0 CHECK (target_triggered IN (0, 1)),
    final_net        TEXT,
    opened_at        TEXT NOT NULL,
    closed_at        TEXT
);
CREATE INDEX cycles_open ON cycles (login, server, state);
