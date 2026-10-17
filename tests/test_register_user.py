import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import register_user


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


if __name__ == "__main__":
    unittest.main()
