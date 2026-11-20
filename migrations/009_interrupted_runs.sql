BEGIN;

ALTER TABLE agent_runs ADD COLUMN resumed_from_run_id INTEGER;
ALTER TABLE agent_runs ADD COLUMN dismissed_at REAL;

CREATE INDEX IF NOT EXISTS idx_agent_runs_dismissed_at
    ON agent_runs(dismissed_at);

COMMIT;
