-- M02 point-in-time candle store (#32; DAT-04, AT-15). Money values are Decimal text.
-- available_at is when the bar became known; as-of queries filter on it.
CREATE TABLE candles (
    dataset_version TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    open_time       TEXT NOT NULL,
    available_at    TEXT NOT NULL,
    body            TEXT NOT NULL,
    PRIMARY KEY (dataset_version, symbol, timeframe, open_time)
);
-- available_at rises with open_time within a series, so one index serves the
-- as-of filter and the newest-first order.
CREATE INDEX candles_as_of ON candles (dataset_version, symbol, timeframe, available_at);

CREATE TRIGGER candles_no_update BEFORE UPDATE ON candles
BEGIN SELECT RAISE(ABORT, 'stored candles are immutable'); END;
CREATE TRIGGER candles_no_delete BEFORE DELETE ON candles
BEGIN SELECT RAISE(ABORT, 'stored candles are immutable'); END;
-- INSERT OR REPLACE would delete and reinsert without firing the triggers above:
-- an insert for an existing key is ignored instead (the store compares content).
CREATE TRIGGER candles_no_replace BEFORE INSERT ON candles
WHEN EXISTS (
    SELECT 1 FROM candles
    WHERE dataset_version = NEW.dataset_version AND symbol = NEW.symbol
      AND timeframe = NEW.timeframe AND open_time = NEW.open_time
)
BEGIN SELECT RAISE(IGNORE); END;
