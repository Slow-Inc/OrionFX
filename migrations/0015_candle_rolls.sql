-- Futures contract rolls per candle dataset version (#128, PRD #120 slice 1). A continuous
-- futures series (FirstRate GC) switches contracts at each roll; the imported series is
-- ratio-adjusted, so rows are not excluded, but the roll dates are kept so a later check
-- or exclusion arm can find the joins. roll_at is the UTC instant the new contract's data
-- starts. A version must have a data source (candle_sources) first. Append-only.
CREATE TABLE candle_rolls (
    dataset_version TEXT NOT NULL,
    roll_at         TEXT NOT NULL,
    from_contract   TEXT NOT NULL,
    to_contract     TEXT NOT NULL,
    PRIMARY KEY (dataset_version, roll_at)
);

CREATE TRIGGER candle_rolls_no_update BEFORE UPDATE ON candle_rolls
BEGIN SELECT RAISE(ABORT, 'candle rolls are append-only'); END;
CREATE TRIGGER candle_rolls_no_delete BEFORE DELETE ON candle_rolls
BEGIN SELECT RAISE(ABORT, 'candle rolls are append-only'); END;
