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

-- Append-only eligibility history; the newest row per subject is current.
CREATE TABLE registry_eligibility (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    subject_ref  TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    record       TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    recorded_by  TEXT NOT NULL
);
CREATE INDEX registry_eligibility_by_subject ON registry_eligibility (kind, subject_ref, id);
