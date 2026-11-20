BEGIN;

CREATE TABLE IF NOT EXISTS skill_imports (
    name        TEXT PRIMARY KEY,
    source_url  TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    hash        TEXT NOT NULL,
    license     TEXT,
    description TEXT,
    imported_at REAL NOT NULL,
    meta        TEXT
);

CREATE INDEX IF NOT EXISTS idx_skill_imports_at ON skill_imports(imported_at DESC);

COMMIT;
