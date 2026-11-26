-- M06 execution ledger and ownership (PRD-06 #13).

-- Every broker write is recorded here PENDING before it is sent.
CREATE TABLE exec_requests (
    request_id         TEXT PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('OPEN', 'CLOSE', 'CANCEL')),
    intent_id          TEXT,
    cycle_id           TEXT,
    symbol             TEXT,
    side               TEXT,
    position_id        TEXT,
    requested_volume   TEXT NOT NULL,
    state              TEXT NOT NULL,
    filled_volume      TEXT,
    fill_price         TEXT,
    broker_position_id TEXT,
    broker_deal_ids    TEXT NOT NULL DEFAULT '[]',
    error              TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX exec_requests_by_state ON exec_requests (state);

-- A broker position is the bot's only if it was opened through a request recorded above.
CREATE TABLE bot_positions (
    position_id TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL REFERENCES exec_requests (request_id),
    cycle_id    TEXT,
    symbol      TEXT NOT NULL,
    linked_at   TEXT NOT NULL
);
