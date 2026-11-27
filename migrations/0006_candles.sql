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
CREATE INDEX candles_as_of ON candles (dataset_version, symbol, timeframe, available_at);

CREATE TRIGGER candles_no_update BEFORE UPDATE ON candles
BEGIN SELECT RAISE(ABORT, 'stored candles are immutable'); END;
