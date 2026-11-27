-- One outcome label per experience (#60): labelling already writes at most one
-- (it skips any experience with a label); this makes the store enforce it, so a
-- reader never has to choose between two kinds for one decision.
CREATE UNIQUE INDEX learning_labels_one_per_decision ON learning_labels (decision_id);
