BEGIN;

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    task TEXT NOT NULL,
    schedule TEXT NOT NULL,
    next_run REAL NOT NULL,
    last_run REAL,
    repeat INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1
);

ALTER TABLE scheduled_tasks ADD COLUMN run_count INTEGER DEFAULT 0;
ALTER TABLE scheduled_tasks ADD COLUMN last_status TEXT;
ALTER TABLE scheduled_tasks ADD COLUMN last_error TEXT;
ALTER TABLE scheduled_tasks ADD COLUMN last_duration_ms INTEGER;
ALTER TABLE scheduled_tasks ADD COLUMN last_result TEXT;

COMMIT;
