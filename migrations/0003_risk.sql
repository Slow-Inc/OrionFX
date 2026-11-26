-- M06 drawdown state (PRD-06 #14). One row per account (login@server).
-- Money values are Decimal text. The lock survives restarts (AT-07).
CREATE TABLE risk_state (
    account_key       TEXT PRIMARY KEY,
    epoch_id          INTEGER NOT NULL,
    epoch_peak        TEXT,
    lifetime_peak     TEXT,
    locked            INTEGER NOT NULL CHECK (locked IN (0, 1)),
    lock_reason       TEXT,
    lifetime_breached INTEGER NOT NULL CHECK (lifetime_breached IN (0, 1)),
    updated_at        TEXT NOT NULL
);
