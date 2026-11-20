BEGIN;

CREATE TABLE IF NOT EXISTS canvas_artifacts (
    slug       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    html       TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    thread_id  TEXT,
    meta       TEXT
);

CREATE INDEX IF NOT EXISTS idx_canvas_artifacts_updated
    ON canvas_artifacts(updated_at DESC);

COMMIT;
