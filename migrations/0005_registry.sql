-- V1.1 versioned registry (#20; master plan §15.5 storage, UST-01, UST-08).
-- One row per (kind, subject, version); a version's artifact hash never changes.
CREATE TABLE registry_artifacts (
    kind          TEXT NOT NULL CHECK (kind IN ('strategy', 'technique', 'composition', 'model')),
    subject_id    TEXT NOT NULL,
    version       TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    manifest      TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    PRIMARY KEY (kind, subject_id, version)
);

-- Append-only eligibility history; the newest row per subject is current. It can
-- only name a registered artifact (the hash match is checked by the registry).
CREATE TABLE registry_eligibility (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('strategy', 'technique', 'composition', 'model')),
    subject_id  TEXT NOT NULL,
    version     TEXT NOT NULL,
    record      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    FOREIGN KEY (kind, subject_id, version)
        REFERENCES registry_artifacts (kind, subject_id, version)
);
CREATE INDEX registry_eligibility_by_subject ON registry_eligibility (kind, subject_id, version, id);

-- History is immutable at the storage layer, not only in code.
CREATE TRIGGER registry_artifacts_no_update BEFORE UPDATE ON registry_artifacts
BEGIN SELECT RAISE(ABORT, 'registry versions are immutable'); END;
CREATE TRIGGER registry_artifacts_no_delete BEFORE DELETE ON registry_artifacts
BEGIN SELECT RAISE(ABORT, 'registry versions are immutable'); END;
CREATE TRIGGER registry_eligibility_no_update BEFORE UPDATE ON registry_eligibility
BEGIN SELECT RAISE(ABORT, 'eligibility history is append-only'); END;
CREATE TRIGGER registry_eligibility_no_delete BEFORE DELETE ON registry_eligibility
BEGIN SELECT RAISE(ABORT, 'eligibility history is append-only'); END;
