CREATE INDEX IF NOT EXISTS idx_messages_thread_ts ON messages(thread_id, ts);
