-- M07 experience log and outcome labels (#59; LRN-02, LRN-06, AT-15).
-- Append-only: an experience is what the bot saw and decided; a label is what was
-- learned later, stamped with when it became known (available_at).
CREATE TABLE learning_experiences (
    decision_id    TEXT PRIMARY KEY,
    action         TEXT NOT NULL,
    strategy_id    TEXT,
    direction      TEXT,
    cycle_id       TEXT,
    mode           TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    snapshot_ref   TEXT NOT NULL,
    as_of          TEXT NOT NULL,
    horizon        TEXT NOT NULL,
    horizon_bars   INTEGER NOT NULL CHECK (horizon_bars > 0),
    horizon_end    TEXT NOT NULL,
    intent_id      TEXT,
    regime         TEXT NOT NULL,
    body           TEXT NOT NULL,
    recorded_at    TEXT NOT NULL
);
CREATE INDEX learning_experiences_horizon_end ON learning_experiences (horizon_end);

CREATE TABLE learning_labels (
    decision_id  TEXT NOT NULL REFERENCES learning_experiences (decision_id),
    kind         TEXT NOT NULL CHECK (kind IN ('REALIZED', 'MARKET_MOVE')),
    value        TEXT NOT NULL,
    direction    TEXT NOT NULL,
    available_at TEXT NOT NULL,
    body         TEXT NOT NULL,
    labeled_at   TEXT NOT NULL,
    PRIMARY KEY (decision_id, kind)
);
CREATE INDEX learning_labels_available ON learning_labels (available_at);

CREATE TRIGGER learning_experiences_no_update BEFORE UPDATE ON learning_experiences
BEGIN SELECT RAISE(ABORT, 'experiences are append-only'); END;
CREATE TRIGGER learning_experiences_no_delete BEFORE DELETE ON learning_experiences
BEGIN SELECT RAISE(ABORT, 'experiences are append-only'); END;
CREATE TRIGGER learning_labels_no_update BEFORE UPDATE ON learning_labels
BEGIN SELECT RAISE(ABORT, 'labels are append-only'); END;
CREATE TRIGGER learning_labels_no_delete BEFORE DELETE ON learning_labels
BEGIN SELECT RAISE(ABORT, 'labels are append-only'); END;

-- A REALIZED label stamps the cycle's close time on its final realized net, so that
-- value must never change once the cycle is CLOSED (point-in-time labels, AT-15).
CREATE TRIGGER cycles_final_net_frozen BEFORE UPDATE OF final_net ON cycles
WHEN OLD.state = 'CLOSED' AND OLD.final_net IS NOT NULL
     AND (NEW.final_net IS NULL OR NEW.final_net != OLD.final_net)
BEGIN SELECT RAISE(ABORT, 'final net of a closed cycle is frozen'); END;
