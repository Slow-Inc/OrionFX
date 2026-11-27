-- M07 model lifecycle (#61; LRN-04/05/06, AT-16). Append-only.
-- A dataset version is immutable; promotions record who approved which champion, so a
-- rollback can restore the previous one with its own evidence.
CREATE TABLE learning_datasets (
    dataset_id TEXT NOT NULL,
    version    TEXT NOT NULL,
    body       TEXT NOT NULL,
    saved_at   TEXT NOT NULL,
    PRIMARY KEY (dataset_id, version)
);

-- A candidate model with the datasets it was trained on (at least one).
CREATE TABLE learning_candidates (
    model_id    TEXT NOT NULL,
    version     TEXT NOT NULL,
    body        TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    PRIMARY KEY (model_id, version)
);

-- The champion-pointer log: which version is champion, by whose approval. The
-- registry's eligibility records stay the authority on what may run.
CREATE TABLE learning_promotions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    TEXT NOT NULL,
    version     TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('PROMOTE', 'ROLLBACK')),
    record      TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approval    TEXT NOT NULL,
    at          TEXT NOT NULL
);
CREATE INDEX learning_promotions_by_model ON learning_promotions (model_id, id);

CREATE TRIGGER learning_datasets_no_update BEFORE UPDATE ON learning_datasets
BEGIN SELECT RAISE(ABORT, 'dataset versions are immutable'); END;
CREATE TRIGGER learning_datasets_no_delete BEFORE DELETE ON learning_datasets
BEGIN SELECT RAISE(ABORT, 'dataset versions are immutable'); END;
CREATE TRIGGER learning_promotions_no_update BEFORE UPDATE ON learning_promotions
BEGIN SELECT RAISE(ABORT, 'promotions are append-only'); END;
CREATE TRIGGER learning_promotions_no_delete BEFORE DELETE ON learning_promotions
BEGIN SELECT RAISE(ABORT, 'promotions are append-only'); END;
CREATE TRIGGER learning_candidates_no_update BEFORE UPDATE ON learning_candidates
BEGIN SELECT RAISE(ABORT, 'candidates are immutable'); END;
CREATE TRIGGER learning_candidates_no_delete BEFORE DELETE ON learning_candidates
BEGIN SELECT RAISE(ABORT, 'candidates are immutable'); END;
