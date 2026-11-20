BEGIN;

ALTER TABLE scheduled_tasks ADD COLUMN budget_usd_cap REAL;
ALTER TABLE scheduled_tasks ADD COLUMN budget_period_sec INTEGER DEFAULT 86400;

COMMIT;
