BEGIN;

CREATE TABLE goal_facts (
    goal_id            TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    key                TEXT NOT NULL,
    value              TEXT NOT NULL,
    source_subtask_id  TEXT,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    PRIMARY KEY (goal_id, key)
);

CREATE INDEX idx_goal_facts_goal ON goal_facts (goal_id);

COMMIT;
