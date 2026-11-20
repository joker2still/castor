ALTER TABLE agent_runs ADD COLUMN goal_id TEXT;

CREATE INDEX IF NOT EXISTS idx_agent_runs_goal_id
    ON agent_runs(goal_id)
    WHERE goal_id IS NOT NULL;
