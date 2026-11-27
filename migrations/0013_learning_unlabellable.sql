-- Recorded bars whose outcome can never be measured (#125, PRD #120 slice 0b): the
-- data has moved past a missing start or end bar. Marked once so `label_due` stops
-- re-querying them. Append-only, like the rest of the M07 dataset.
CREATE TABLE learning_unlabellable (
    symbol          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    horizon_bars    INTEGER NOT NULL CHECK (horizon_bars > 0),
    dataset_version TEXT NOT NULL,
    reason          TEXT NOT NULL,
    marked_at       TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of, horizon, horizon_bars, dataset_version)
);

CREATE TRIGGER learning_unlabellable_no_update BEFORE UPDATE ON learning_unlabellable
BEGIN SELECT RAISE(ABORT, 'unlabellable marks are append-only'); END;
CREATE TRIGGER learning_unlabellable_no_delete BEFORE DELETE ON learning_unlabellable
BEGIN SELECT RAISE(ABORT, 'unlabellable marks are append-only'); END;
