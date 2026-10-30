import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import register_user
import license_admin
from license_server.database import Database
from license_server.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
    legacy_adoption_name,
    schema_contract_checksum,
)


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


class RegistrationTests(unittest.TestCase):
    def test_schema_is_compatible_with_el9_sqlite(self):
        self.assertEqual(register_user.MIN_SQLITE_VERSION, (3, 34, 1))
        schema = register_user.SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(schema, r"\)\s+STRICT\s*;")
        for migration in MIGRATIONS:
            self.assertNotRegex(
                migration.path.read_text(encoding="utf-8"), r"\)\s+STRICT\s*;"
            )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "license.sqlite3"
        register_user.initialize_database(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_and_seed_plans(self):
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                con.execute("PRAGMA user_version").fetchone()[0],
                LATEST_SCHEMA_VERSION,
            )
            migrations = con.execute(
                "SELECT version,name,sha256 FROM schema_migrations ORDER BY version"
            ).fetchall()
            plans = {row[0] for row in con.execute("SELECT id FROM plans")}
        self.assertEqual(
            migrations,
            [
                (migration.version, migration.name, migration.checksum())
                for migration in MIGRATIONS
            ],
        )
        self.assertEqual(plans, set(register_user.PLAN_IDS))
        if os.name != "nt":
            self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_v1_database_is_migrated_without_losing_existing_rows(self):
        legacy = Path(self.temp.name) / "legacy-v1.sqlite3"
        with sqlite3.connect(legacy) as con:
            con.executescript(
                (FIXTURE_ROOT / "legacy_v1_schema.sql").read_text(
                    encoding="utf-8"
                )
            )
            register_user.seed_plans(con, "2026-10-01T00:00:00Z")
            con.execute(
                "INSERT INTO customers(id,username,username_norm,display_name,status,"
                "created_at,updated_at) VALUES('customer-1','Alice','alice','Alice',"
                "'active','2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"
            )
            con.execute(
                "INSERT INTO entitlements(id,customer_id,plan_id,status,starts_at,"
                "plan_snapshot_json,created_at,updated_at) VALUES("
                "'entitlement-1','customer-1','free','active',"
                "'2026-10-01T00:00:00Z','{\"device_limit\":1}',"
                "'2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')"
            )
            con.execute(
                "INSERT INTO activation_credentials(id,customer_id,entitlement_id,"
                "token_prefix,secret_hash,status,created_at) VALUES("
                "'credential-1','customer-1','entitlement-1','WKM1-LEGACY',"
                "'legacy-secret-hash','active','2026-10-01T00:00:00Z')"
            )
            con.execute(
                "INSERT INTO devices(id,entitlement_id,install_id,device_hash,"
                "platform,app_version,status,activated_at,last_seen_at) VALUES("
                "'device-1','entitlement-1','legacy-install','legacy-device-hash',"
                "'linux','0.1.0','active','2026-10-01T00:00:00Z',"
                "'2026-10-02T00:00:00Z')"
            )
            con.execute(
                "INSERT INTO audit_events(id,actor_type,actor_id,event_type,"
                "subject_type,subject_id,data_json,created_at) VALUES("
                "7,'system','','legacy.created','device','device-1','{}',"
                "'2026-10-01T00:00:00Z')"
            )
            con.commit()

        result = register_user.initialize_database(legacy)
        self.assertEqual(result["schema_version"], LATEST_SCHEMA_VERSION)
        with sqlite3.connect(legacy) as con:
            self.assertEqual(
                con.execute("PRAGMA user_version").fetchone()[0],
                LATEST_SCHEMA_VERSION,
            )
            self.assertEqual(
                con.execute("SELECT username FROM customers").fetchone()[0], "Alice"
            )
            self.assertEqual(
                con.execute(
                    "SELECT id,status,plan_snapshot_json FROM entitlements"
                ).fetchall(),
                [("entitlement-1", "active", '{"device_limit":1}')],
            )
            self.assertEqual(
                {
                    row[0]
                    for row in con.execute("SELECT id FROM plans")
                },
                set(register_user.PLAN_IDS),
            )
            self.assertEqual(
                con.execute(
                    "SELECT id,token_prefix,status FROM activation_credentials"
                ).fetchall(),
                [("credential-1", "WKM1-LEGACY", "active")],
            )
            self.assertEqual(
                con.execute(
                    "SELECT id,last_seen_at FROM devices"
                ).fetchall(),
                [("device-1", "2026-10-02T00:00:00Z")],
            )
            self.assertEqual(
                con.execute(
                    "SELECT id,event_type FROM audit_events WHERE id=7"
                ).fetchall(),
                [(7, "legacy.created")],
            )
            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='refresh_sessions'"
                ).fetchone()
            )
            self.assertEqual(
                con.execute(
                    "SELECT version,name,sha256 FROM schema_migrations "
                    "ORDER BY version"
                ).fetchall(),
                [
                    (
                        1,
                        legacy_adoption_name(1),
                        schema_contract_checksum(1),
                    ),
                    *[
                        (migration.version, migration.name, migration.checksum())
                        for migration in MIGRATIONS[1:]
                    ],
                ],
            )
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_incomplete_v2_database_fails_closed_and_is_not_ready(self):
        with sqlite3.connect(self.db) as con:
            con.execute("DROP TABLE schema_migrations")
            con.execute("DROP TABLE refresh_sessions")
            con.commit()

        with self.assertRaisesRegex(MigrationError, "最低结构合同"):
            register_user.initialize_database(self.db)

        database = Database(self.db)
        self.assertFalse(database.ready())
        self.assertFalse(database.deep_ready())
        with sqlite3.connect(self.db) as con:
            self.assertIsNone(
                con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='schema_migrations'"
                ).fetchone()
            )

    def test_existing_migration_ledger_is_not_silently_backfilled(self):
        with sqlite3.connect(self.db) as con:
            con.execute("DELETE FROM schema_migrations WHERE version=1")
            con.commit()

        with self.assertRaisesRegex(MigrationError, "版本集"):
            register_user.initialize_database(self.db)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(
                con.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall(),
                [(migration.version,) for migration in MIGRATIONS[1:]],
            )

    def test_failed_pending_migration_rolls_back_the_entire_batch(self):
        good = Path(self.temp.name) / "001-good.sql"
        bad = Path(self.temp.name) / "002-bad.sql"
        good.write_text(
            "CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);\n"
            "INSERT INTO schema_meta VALUES('schema_version','1');\n"
            "CREATE TABLE first_table(id INTEGER PRIMARY KEY);\n"
            "PRAGMA user_version=1;\n",
            encoding="utf-8",
        )
        bad.write_text(
            "CREATE TABLE partial_table(id INTEGER PRIMARY KEY);\n"
            "UPDATE schema_meta SET value='2' WHERE key='schema_version';\n"
            "PRAGMA user_version=2;\n"
            "THIS IS NOT VALID SQL;\n",
            encoding="utf-8",
        )
        failed = Path(self.temp.name) / "failed.sqlite3"
        with sqlite3.connect(failed) as con:
            with self.assertRaises(MigrationError):
                apply_migrations(
                    con,
                    applied_at="2026-10-30T00:00:00Z",
                    migrations=(
                        Migration(1, "good", good),
                        Migration(2, "bad", bad),
                    ),
                )
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("schema_meta", tables)
            self.assertNotIn("schema_migrations", tables)
            self.assertNotIn("first_table", tables)
            self.assertNotIn("partial_table", tables)
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_applied_migration_files_are_immutable(self):
        migration_path = Path(self.temp.name) / "001-immutable.sql"
        migration_path.write_text(
            "CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);\n"
            "INSERT INTO schema_meta VALUES('schema_version','1');\n"
            "PRAGMA user_version=1;\n",
            encoding="utf-8",
        )
        database = Path(self.temp.name) / "immutable.sqlite3"
        migration = Migration(1, "immutable", migration_path)
        with sqlite3.connect(database) as con:
            apply_migrations(
                con,
                applied_at="2026-10-30T00:00:00Z",
                migrations=(migration,),
            )
            migration_path.write_text(
                migration_path.read_text(encoding="utf-8") + "-- modified\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MigrationError, "SHA-256"):
                apply_migrations(
                    con,
                    applied_at="2026-10-30T00:01:00Z",
                    migrations=(migration,),
                )

    def test_register_permanent_user_and_never_store_plaintext_code(self):
        result = register_user.register_user(
            self.db, "Alice", "Alice", "permanent_personal", None)
        code = result["activation_code"]
        expected = hashlib.sha256(
            ("wechat-memory-license:v1:" + code).encode("utf-8")
        ).hexdigest()
        with sqlite3.connect(self.db) as con:
            customer = con.execute(
                "SELECT username, username_norm FROM customers"
            ).fetchone()
            stored = con.execute(
                "SELECT secret_hash FROM activation_credentials"
            ).fetchone()[0]
            expires_at, snapshot = con.execute(
                "SELECT expires_at, plan_snapshot_json FROM entitlements"
            ).fetchone()
            event = con.execute("SELECT event_type FROM audit_events").fetchone()[0]
            database_bytes = self.db.read_bytes()
        self.assertEqual(customer, ("Alice", "alice"))
        self.assertEqual(stored, expected)
        self.assertIsNone(expires_at)
        self.assertEqual(json.loads(snapshot)["device_limit"], 2)
        self.assertEqual(event, "customer.registered")
        self.assertNotIn(code.encode("utf-8"), database_bytes)

    def test_duplicate_username_is_casefolded(self):
        register_user.register_user(self.db, "Alice", "", "free", None)
        with self.assertRaisesRegex(register_user.RegistrationError, "用户名已经存在"):
            register_user.register_user(self.db, "ＡＬＩＣＥ", "", "free", None)

    def test_subscription_requires_days(self):
        with self.assertRaisesRegex(register_user.RegistrationError, "必须提供 --days"):
            register_user.register_user(
                self.db, "subscriber", "", "pro_subscription", None)
        result = register_user.register_user(
            self.db, "subscriber", "", "pro_subscription", 365)
        self.assertIsNotNone(result["expires_at"])
        self.assertEqual(result["plan"]["device_limit"], 3)
        self.assertIn("standard_analytics", result["plan"]["features"])
        self.assertIn("advanced_analytics", result["plan"]["features"])

    def test_no_email_phone_or_payment_fields(self):
        with sqlite3.connect(self.db) as con:
            customer_columns = {
                row[1] for row in con.execute("PRAGMA table_info(customers)")
            }
            tables = {
                row[0] for row in con.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
        self.assertNotIn("email", customer_columns)
        self.assertNotIn("phone", customer_columns)
        self.assertFalse(any("payment" in table for table in tables))

    def test_admin_changes_plan_limits_and_subscription_expiry(self):
        register_user.register_user(self.db, "Alice", "", "free", None)
        changed = license_admin.set_plan(
            self.db,
            "alice",
            "pro_subscription",
            30,
            device_limit=5,
            offline_days=14,
        )
        self.assertEqual(changed["plan_id"], "pro_subscription")
        self.assertEqual(changed["plan"]["device_limit"], 5)
        self.assertEqual(changed["plan"]["offline_grace_days"], 14)
        first_expiry = changed["expires_at"]

        extended = license_admin.extend_subscription(self.db, "Alice", 15)
        self.assertGreater(extended["expires_at"], first_expiry)
        events = []
        with sqlite3.connect(self.db) as con:
            events = [
                row[0]
                for row in con.execute(
                    "SELECT event_type FROM audit_events ORDER BY id"
                )
            ]
        self.assertIn("entitlement.plan_changed", events)
        self.assertIn("entitlement.extended", events)

    def test_admin_does_not_silently_exceed_reduced_device_limit(self):
        registered = register_user.register_user(
            self.db, "Alice", "", "permanent_personal", None
        )
        now = "2026-10-30T00:00:00Z"
        with sqlite3.connect(self.db) as con:
            for index in range(2):
                con.execute(
                    "INSERT INTO devices(id,entitlement_id,install_id,device_hash,status,"
                    "activated_at,last_seen_at) VALUES(?,?,?,?, 'active',?,?)",
                    (
                        f"device-{index}",
                        registered["entitlement_id"],
                        f"install-{index}",
                        str(index) * 64,
                        now,
                        f"2026-10-{29 + index:02d}T00:00:00Z",
                    ),
                )
        with self.assertRaisesRegex(register_user.RegistrationError, "当前有 2 台"):
            license_admin.set_limits(self.db, "alice", 1, None)

        result = license_admin.set_limits(
            self.db, "alice", 1, None, deactivate_excess=True
        )
        self.assertEqual(result["active_devices"], 1)
        active = [item for item in result["devices"] if item["status"] == "active"]
        self.assertEqual(active[0]["device_id"], "device-1")

    def test_reset_code_revokes_old_code_and_refresh_sessions(self):
        registered = register_user.register_user(
            self.db, "Alice", "", "permanent_personal", None
        )
        now = "2026-10-30T00:00:00Z"
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO devices(id,entitlement_id,install_id,device_hash,status,"
                "activated_at,last_seen_at) VALUES('device-1',?,?,?,'active',?,?)",
                (registered["entitlement_id"], "install-1", "a" * 64, now, now),
            )
            con.execute(
                "INSERT INTO refresh_sessions(id,device_id,token_hash,status,created_at,"
                "expires_at) VALUES('refresh-1','device-1','hash-1','active',?,?)",
                (now, "2027-10-30T00:00:00Z"),
            )

        result = license_admin.reset_activation_code(self.db, "Alice")
        code = result["activation_code"]
        with sqlite3.connect(self.db) as con:
            credentials = con.execute(
                "SELECT status,secret_hash FROM activation_credentials ORDER BY created_at,id"
            ).fetchall()
            session_status = con.execute(
                "SELECT status FROM refresh_sessions WHERE id='refresh-1'"
            ).fetchone()[0]
            database_bytes = self.db.read_bytes()
        self.assertEqual(sorted(row[0] for row in credentials), ["active", "revoked"])
        self.assertEqual(session_status, "revoked")
        self.assertNotIn(code.encode(), database_bytes)

    def test_device_block_is_scoped_to_user_and_audited(self):
        registered = register_user.register_user(
            self.db, "Alice", "", "permanent_personal", None
        )
        now = "2026-10-30T00:00:00Z"
        with sqlite3.connect(self.db) as con:
            con.execute(
                "INSERT INTO devices(id,entitlement_id,install_id,device_hash,status,"
                "activated_at,last_seen_at) VALUES('device-1',?,?,?,'active',?,?)",
                (registered["entitlement_id"], "install-1", "a" * 64, now, now),
            )
        result = license_admin.set_device_status(
            self.db, "alice", "device-1", "blocked"
        )
        self.assertEqual(result["devices"][0]["status"], "blocked")
        with sqlite3.connect(self.db) as con:
            event = con.execute(
                "SELECT event_type FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(event, "device.status_changed")


if __name__ == "__main__":
    unittest.main()
