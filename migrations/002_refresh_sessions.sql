CREATE TABLE refresh_sessions (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('active', 'rotated', 'revoked', 'expired')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_used_at TEXT,
    rotated_at TEXT,
    revoked_at TEXT,
    client_version TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_refresh_sessions_device
    ON refresh_sessions(device_id, status, expires_at);

UPDATE schema_meta SET value='2' WHERE key='schema_version';
PRAGMA user_version = 2;
