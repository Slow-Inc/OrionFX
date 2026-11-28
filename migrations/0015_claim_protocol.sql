-- X02 claim protocol machinery (#132, PRD #120 slice 5; training-loop.md §6).
-- (0015, not 0014: 0014_candle_sources.sql is taken on the #128 branch.)
-- Protocols are frozen by content hash. Every run is kept, including failures, A/A and
-- bug-fix reruns; errata mark a run without removing it. The holdout opens once per
-- protocol (the primary key). Nothing here is ever updated or deleted.
CREATE TABLE claim_protocols (
    protocol_hash TEXT PRIMARY KEY,
    version       TEXT NOT NULL UNIQUE,
    body          TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE claim_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_hash TEXT NOT NULL REFERENCES claim_protocols (protocol_hash),
    config_id     TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'bugfix-rerun', 'aa')),
    body          TEXT NOT NULL,
    recorded_at   TEXT NOT NULL
);
CREATE INDEX claim_runs_by_config ON claim_runs (protocol_hash, config_hash);

CREATE TABLE claim_holdout_openings (
    protocol_hash TEXT PRIMARY KEY REFERENCES claim_protocols (protocol_hash),
    config_id     TEXT NOT NULL,
    opened_at     TEXT NOT NULL
);

CREATE TABLE claim_run_errata (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER NOT NULL REFERENCES claim_runs (run_id),
    state     TEXT NOT NULL
              CHECK (state IN ('INVALID', 'WITHDRAWN', 'CORRECTED', 'UNCONFIRMED', 'SUPERSEDED')),
    reason    TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    marked_at TEXT NOT NULL
);
CREATE INDEX claim_run_errata_by_run ON claim_run_errata (run_id);

CREATE TRIGGER claim_protocols_no_update BEFORE UPDATE ON claim_protocols
BEGIN SELECT RAISE(ABORT, 'a protocol version is immutable'); END;
CREATE TRIGGER claim_protocols_no_delete BEFORE DELETE ON claim_protocols
BEGIN SELECT RAISE(ABORT, 'a protocol version is immutable'); END;
CREATE TRIGGER claim_runs_no_update BEFORE UPDATE ON claim_runs
BEGIN SELECT RAISE(ABORT, 'claim runs are append-only'); END;
CREATE TRIGGER claim_runs_no_delete BEFORE DELETE ON claim_runs
BEGIN SELECT RAISE(ABORT, 'claim runs are append-only'); END;
CREATE TRIGGER claim_holdout_openings_no_update BEFORE UPDATE ON claim_holdout_openings
BEGIN SELECT RAISE(ABORT, 'a holdout opening is permanent'); END;
CREATE TRIGGER claim_holdout_openings_no_delete BEFORE DELETE ON claim_holdout_openings
BEGIN SELECT RAISE(ABORT, 'a holdout opening is permanent'); END;
CREATE TRIGGER claim_run_errata_no_update BEFORE UPDATE ON claim_run_errata
BEGIN SELECT RAISE(ABORT, 'errata are append-only'); END;
CREATE TRIGGER claim_run_errata_no_delete BEFORE DELETE ON claim_run_errata
BEGIN SELECT RAISE(ABORT, 'errata are append-only'); END;
