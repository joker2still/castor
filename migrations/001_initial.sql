CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    name TEXT,
    ts REAL NOT NULL,
    thread_id TEXT DEFAULT 'default',
    meta TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS presets (
    id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    author_name TEXT,
    license_type TEXT,
    manifest_json TEXT NOT NULL,
    installed_at REAL NOT NULL,
    source_path TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived INTEGER DEFAULT 0,
    meta TEXT DEFAULT '{}'
);

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

CREATE TABLE IF NOT EXISTS secrets (
    key TEXT PRIMARY KEY,
    value BLOB NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_rag USING fts5(
    chunk_id, file_path, text,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_memory USING fts5(
    point_id, tag, text,
    tokenize='porter unicode61'
);
