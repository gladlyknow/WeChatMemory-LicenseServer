import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import register_user
import license_admin


class RegistrationTests(unittest.TestCase):
    def test_schema_is_compatible_with_el9_sqlite(self):
        self.assertEqual(register_user.MIN_SQLITE_VERSION, (3, 34, 1))
        schema = register_user.SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(schema, r"\)\s+STRICT\s*;")

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
            plans = {row[0] for row in con.execute("SELECT id FROM plans")}
        self.assertEqual(plans, set(register_user.PLAN_IDS))
        if os.name != "nt":
            self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

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
