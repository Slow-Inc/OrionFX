-- X02 evidence store, readiness policies and live qualifications (#68; VAL-04/05/06).
-- Append-only: evidence cannot be edited after the fact, a policy version cannot be
-- changed in place, and a qualification is bound to the policy version it was earned under.
CREATE TABLE validation_runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    body         TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);

CREATE TABLE readiness_policies (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    version  TEXT NOT NULL UNIQUE,
    body     TEXT NOT NULL,
    hash     TEXT NOT NULL,
    saved_at TEXT NOT NULL
);

CREATE TABLE live_qualifications (
    qualification_id TEXT PRIMARY KEY,
    policy_version   TEXT NOT NULL REFERENCES readiness_policies (version),
    body             TEXT NOT NULL,
    issued_at        TEXT NOT NULL
);

CREATE TRIGGER validation_runs_no_update BEFORE UPDATE ON validation_runs
BEGIN SELECT RAISE(ABORT, 'validation evidence is append-only'); END;
CREATE TRIGGER validation_runs_no_delete BEFORE DELETE ON validation_runs
BEGIN SELECT RAISE(ABORT, 'validation evidence is append-only'); END;
CREATE TRIGGER readiness_policies_no_update BEFORE UPDATE ON readiness_policies
BEGIN SELECT RAISE(ABORT, 'a readiness policy version is immutable'); END;
CREATE TRIGGER readiness_policies_no_delete BEFORE DELETE ON readiness_policies
BEGIN SELECT RAISE(ABORT, 'a readiness policy version is immutable'); END;
CREATE TRIGGER live_qualifications_no_update BEFORE UPDATE ON live_qualifications
BEGIN SELECT RAISE(ABORT, 'qualifications are append-only'); END;
CREATE TRIGGER live_qualifications_no_delete BEFORE DELETE ON live_qualifications
BEGIN SELECT RAISE(ABORT, 'qualifications are append-only'); END;
