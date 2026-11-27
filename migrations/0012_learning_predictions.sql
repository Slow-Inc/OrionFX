-- M07 dataset: what each loaded prediction model said on each recorded bar (#109,
-- PRD #107). Learning data only: never journaled as prediction.generated, so it never
-- reaches trading. Append-only; one row per (bar, model version). Provenance is the
-- bar's: a BACKFILL prediction may be in-sample for a model trained on those candles, so
-- an evaluation must tell them apart.
CREATE TABLE learning_predictions (
    symbol          TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    feature_schema  TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    horizon_bars    INTEGER NOT NULL CHECK (horizon_bars > 0),
    direction       TEXT NOT NULL,
    provenance      TEXT NOT NULL CHECK (provenance IN ('LIVE', 'BACKFILL')),
    body            TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of, feature_schema, dataset_version, model_id, model_version)
);
CREATE INDEX learning_predictions_by_model ON learning_predictions (symbol, model_id, as_of);

CREATE TRIGGER learning_predictions_no_update BEFORE UPDATE ON learning_predictions
BEGIN SELECT RAISE(ABORT, 'predictions are append-only'); END;
CREATE TRIGGER learning_predictions_no_delete BEFORE DELETE ON learning_predictions
BEGIN SELECT RAISE(ABORT, 'predictions are append-only'); END;
