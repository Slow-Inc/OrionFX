-- M07 autonomous champion/candidate loop (#133, PRD #120 slice 6, ADR 0001).
-- (0017: 0014 and 0015 are taken on the #128 branch, 0016 on the #132 branch.)
--
-- 1. The champion-pointer log gains WITHDRAW (rollback to no model: M05 waits) and RENEW
--    (a healthy champion's approval extended). SQLite cannot alter a CHECK, so the table
--    is rebuilt with its rows, index and append-only triggers. DROP TABLE removes the old
--    triggers before its implicit delete, so no trigger fires.
CREATE TABLE learning_promotions_v2 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    TEXT NOT NULL,
    version     TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('PROMOTE', 'ROLLBACK', 'WITHDRAW', 'RENEW')),
    record      TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approval    TEXT NOT NULL,
    at          TEXT NOT NULL
);
INSERT INTO learning_promotions_v2 (id, model_id, version, action, record, approved_by,
                                    approval, at)
SELECT id, model_id, version, action, record, approved_by, approval, at
FROM learning_promotions;
DROP TABLE learning_promotions;
ALTER TABLE learning_promotions_v2 RENAME TO learning_promotions;
CREATE INDEX learning_promotions_by_model ON learning_promotions (model_id, id);
CREATE TRIGGER learning_promotions_no_update BEFORE UPDATE ON learning_promotions
BEGIN SELECT RAISE(ABORT, 'promotions are append-only'); END;
CREATE TRIGGER learning_promotions_no_delete BEFORE DELETE ON learning_promotions
BEGIN SELECT RAISE(ABORT, 'promotions are append-only'); END;

-- 2. Every autonomous step, with its full audit record (actor, policy and protocol hash,
--    evidence refs, the champion before and after, reasons and metrics) as JSON.
--    Append-only: the operator's history of what the loop did and why.
CREATE TABLE learning_autonomy (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    version  TEXT NOT NULL,
    action   TEXT NOT NULL CHECK (action IN ('ADMIT', 'REFUSE', 'PROMOTE', 'REJECT',
                                             'EXPIRE', 'ROLLBACK', 'WITHDRAW', 'RENEW')),
    body     TEXT NOT NULL,
    at       TEXT NOT NULL
);
CREATE INDEX learning_autonomy_by_model ON learning_autonomy (model_id, version, id);
CREATE TRIGGER learning_autonomy_no_update BEFORE UPDATE ON learning_autonomy
BEGIN SELECT RAISE(ABORT, 'autonomy records are append-only'); END;
CREATE TRIGGER learning_autonomy_no_delete BEFORE DELETE ON learning_autonomy
BEGIN SELECT RAISE(ABORT, 'autonomy records are append-only'); END;
