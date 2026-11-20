BEGIN;

ALTER TABLE scheduled_tasks ADD COLUMN thread_id TEXT;

COMMIT;
