BEGIN;

CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       TEXT NOT NULL,
    cron_id         INTEGER,
    source          TEXT NOT NULL,
    scheduled_at    REAL,
    started_at      REAL NOT NULL,
    finished_at     REAL,
    duration_ms     INTEGER,
    status          TEXT NOT NULL,
    error           TEXT,
    result_preview  TEXT,
    model           TEXT,
    provider        TEXT,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cost_usd        REAL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_thread_id  ON agent_runs(thread_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at ON agent_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_cron_id    ON agent_runs(cron_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_source     ON agent_runs(source);

INSERT INTO agent_runs
    (cron_id, thread_id, scheduled_at, started_at, finished_at,
     duration_ms, status, error, result_preview, source)
SELECT cron_id, COALESCE(thread_id, ''), scheduled_at,
       COALESCE(started_at, scheduled_at, 0.0),
       finished_at, duration_ms, status, error, result_preview, 'routine'
FROM routine_runs;

DROP TABLE IF EXISTS routine_runs;

COMMIT;
