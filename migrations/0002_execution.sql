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
    order_id           TEXT,
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

-- A broker position is the bot's only if it was opened through a request recorded
-- above, on this account/server, with the bot magic (checked by the caller).
CREATE TABLE bot_positions (
    position_id TEXT NOT NULL,
    login       TEXT NOT NULL,
    server      TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    request_id  TEXT NOT NULL REFERENCES exec_requests (request_id),
    cycle_id    TEXT,
    linked_at   TEXT NOT NULL,
    PRIMARY KEY (login, server, position_id)
);

-- Pending orders the bot placed itself. MVP places none; cancel refuses anything else.
CREATE TABLE bot_orders (
    order_id   TEXT NOT NULL,
    login      TEXT NOT NULL,
    server     TEXT NOT NULL,
    request_id TEXT NOT NULL REFERENCES exec_requests (request_id),
    linked_at  TEXT NOT NULL,
    PRIMARY KEY (login, server, order_id)
);
