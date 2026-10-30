-- Latest schema snapshot for review and tooling. Runtime initialization and
-- upgrades are driven by the ordered, transactional files in migrations/.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    -- Native rows identify immutable SQL files. Pre-ledger databases instead
    -- use legacy-schema-vN-adopted and hash a versioned structure contract.
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    applied_at TEXT NOT NULL
);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '3')
ON CONFLICT(key) DO UPDATE SET value = excluded.value;

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_norm TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    billing_kind TEXT NOT NULL CHECK (billing_kind IN ('free', 'permanent', 'subscription')),
    device_limit INTEGER NOT NULL CHECK (device_limit > 0),
    offline_grace_days INTEGER NOT NULL CHECK (offline_grace_days >= 0),
    upgrade_days INTEGER CHECK (upgrade_days IS NULL OR upgrade_days >= 0),
    features_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entitlements (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended', 'expired', 'revoked')),
    starts_at TEXT NOT NULL,
    expires_at TEXT,
    upgrade_until TEXT,
    plan_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (expires_at IS NULL OR expires_at > starts_at)
);

CREATE TABLE IF NOT EXISTS activation_credentials (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
    entitlement_id TEXT NOT NULL REFERENCES entitlements(id) ON DELETE RESTRICT,
    token_prefix TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    entitlement_id TEXT NOT NULL REFERENCES entitlements(id) ON DELETE RESTRICT,
    install_id TEXT NOT NULL,
    device_hash TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'deactivated', 'blocked')),
    activated_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    deactivated_at TEXT,
    UNIQUE(entitlement_id, install_id)
);

CREATE TABLE IF NOT EXISTS refresh_sessions (
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

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entitlements_customer
    ON entitlements(customer_id, status);
CREATE INDEX IF NOT EXISTS idx_devices_entitlement
    ON devices(entitlement_id, status);
CREATE INDEX IF NOT EXISTS idx_refresh_sessions_device
    ON refresh_sessions(device_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_refresh_sessions_rotated_retention
    ON refresh_sessions(rotated_at, id)
    WHERE status='rotated' AND rotated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_refresh_sessions_revoked_retention
    ON refresh_sessions(revoked_at, id)
    WHERE status='revoked' AND revoked_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_subject
    ON audit_events(subject_type, subject_id, created_at);

PRAGMA user_version = 3;
