CREATE INDEX idx_refresh_sessions_rotated_retention
    ON refresh_sessions(rotated_at, id)
    WHERE status='rotated' AND rotated_at IS NOT NULL;

CREATE INDEX idx_refresh_sessions_revoked_retention
    ON refresh_sessions(revoked_at, id)
    WHERE status='revoked' AND revoked_at IS NOT NULL;

UPDATE schema_meta SET value='3' WHERE key='schema_version';
PRAGMA user_version = 3;
