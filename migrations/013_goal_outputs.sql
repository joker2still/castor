BEGIN;

CREATE TABLE goal_outputs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    value       TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

CREATE INDEX idx_goal_outputs_goal ON goal_outputs (goal_id);

COMMIT;
