-- Data source per candle dataset version (#128, PRD #120 slice 1). A dataset version holds
-- the bars of one data source (a vendor, or the live broker feed). Stored bars and learning
-- snapshot rows are both keyed by dataset_version, so each maps to one source. The importer
-- registers and checks the binding; the candles table itself does not enforce it.
-- This dimension is separate from LIVE/BACKFILL provenance. schedule_id names the session
-- schedule the bars were validated against. Append-only.
CREATE TABLE candle_sources (
    dataset_version TEXT PRIMARY KEY,
    data_source     TEXT NOT NULL,
    schedule_id     TEXT NOT NULL,
    registered_at   TEXT NOT NULL
);

CREATE TRIGGER candle_sources_no_update BEFORE UPDATE ON candle_sources
BEGIN SELECT RAISE(ABORT, 'candle sources are append-only'); END;
CREATE TRIGGER candle_sources_no_delete BEFORE DELETE ON candle_sources
BEGIN SELECT RAISE(ABORT, 'candle sources are append-only'); END;
