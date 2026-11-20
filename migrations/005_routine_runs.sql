BEGIN;

CREATE TABLE IF NOT EXISTS routine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cron_id INTEGER NOT NULL,
    scheduled_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    duration_ms INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    result_preview TEXT,
    thread_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_routine_runs_cron_id ON routine_runs(cron_id);
CREATE INDEX IF NOT EXISTS idx_routine_runs_scheduled_at ON routine_runs(scheduled_at);

COMMIT;
