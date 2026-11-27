-- M07 training dataset from real operation (#108, PRD #107).
-- Per-bar feature snapshots (LIVE or BACKFILL) and per-bar outcome labels: the raw
-- close-to-close move over a horizon, stamped with when it became known (available_at).
-- Both are append-only; a row is written once per key.
CREATE TABLE learning_snapshots (
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    feature_schema  TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    provenance      TEXT NOT NULL CHECK (provenance IN ('LIVE', 'BACKFILL')),
    mode            TEXT,
    quality         TEXT NOT NULL,
    available_at    TEXT NOT NULL,
    body            TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe, as_of, feature_schema, dataset_version)
);

CREATE INDEX learning_snapshots_by_version ON learning_snapshots (symbol, dataset_version, as_of);

CREATE TABLE learning_outcomes (
    symbol          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    horizon_bars    INTEGER NOT NULL CHECK (horizon_bars > 0),
    dataset_version TEXT NOT NULL,
    move            TEXT NOT NULL,
    spread          TEXT,
    available_at    TEXT NOT NULL,
    body            TEXT NOT NULL,
    labeled_at      TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of, horizon, horizon_bars, dataset_version)
);
CREATE INDEX learning_outcomes_available ON learning_outcomes (available_at);

CREATE TRIGGER learning_snapshots_no_update BEFORE UPDATE ON learning_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are append-only'); END;
CREATE TRIGGER learning_snapshots_no_delete BEFORE DELETE ON learning_snapshots
BEGIN SELECT RAISE(ABORT, 'snapshots are append-only'); END;
CREATE TRIGGER learning_outcomes_no_update BEFORE UPDATE ON learning_outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes are append-only'); END;
CREATE TRIGGER learning_outcomes_no_delete BEFORE DELETE ON learning_outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes are append-only'); END;
