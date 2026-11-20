BEGIN;

CREATE TABLE goals (
    id                TEXT PRIMARY KEY,
    thread_id         TEXT,
    source            TEXT NOT NULL,
    user_input        TEXT NOT NULL,
    status            TEXT NOT NULL,
    plan              TEXT,
    result            TEXT,
    error             TEXT,
    budget_usd        REAL,
    budget_seconds    INTEGER,
    cost_usd          REAL DEFAULT 0,
    started_at        REAL,
    finished_at       REAL,
    created_at        REAL NOT NULL,

    worker_id         TEXT,
    lease_expires_at  REAL,

    meta              TEXT
);

CREATE INDEX idx_goals_status_lease ON goals (status, lease_expires_at);
CREATE INDEX idx_goals_thread ON goals (thread_id);
CREATE INDEX idx_goals_created ON goals (created_at);

CREATE TABLE goal_checkpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id         TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    round_num       INTEGER NOT NULL,
    subtask_index   INTEGER NOT NULL DEFAULT -1,
    messages_blob   BLOB NOT NULL,
    plan_snapshot   TEXT NOT NULL,
    facts_snapshot  TEXT NOT NULL DEFAULT '{}',
    timestamp       REAL NOT NULL,
    UNIQUE (goal_id, round_num)
);

CREATE INDEX idx_checkpoints_goal_round ON goal_checkpoints (goal_id, round_num DESC);

CREATE TABLE goal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    timestamp   REAL NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_events_goal_time ON goal_events (goal_id, timestamp);

COMMIT;
