from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import license_admin
import register_user
from license_server.maintenance import (
    MAX_REFRESH_SESSION_BATCH_SIZE,
    MAX_REFRESH_SESSION_MAX_BATCHES,
)
from license_server.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    apply_migrations,
)


NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-01-01T00:00:00Z"
RECENT = "2026-10-15T00:00:00Z"
CUTOFF = "2026-09-30T12:00:00Z"
FUTURE = "2027-10-30T00:00:00Z"


class RefreshSessionRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "license.sqlite3"
        register_user.initialize_database(self.db)
        registration = register_user.register_user(
            self.db, "Alice", "", "permanent_personal", None
        )
        self.entitlement_id = registration["entitlement_id"]
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO devices(id,entitlement_id,install_id,device_hash,status,"
                "activated_at,last_seen_at) VALUES(" 
                "'device-1',?,?,?,'active',?,?)",
                (
                    self.entitlement_id,
                    "install-1",
                    "a" * 64,
                    OLD,
                    RECENT,
                ),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _insert(
        self,
        identifier: str,
        status: str,
        *,
        rotated_at: str | None = None,
        revoked_at: str | None = None,
    ) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO refresh_sessions("
                "id,device_id,token_hash,status,created_at,expires_at,rotated_at,revoked_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    "device-1",
                    f"hash-{identifier}",
                    status,
                    OLD,
                    FUTURE,
                    rotated_at,
                    revoked_at,
                ),
            )

    def test_only_old_rotated_and_revoked_rows_are_deleted(self) -> None:
        self._insert("old-rotated", "rotated", rotated_at=OLD)
        self._insert("old-revoked", "revoked", revoked_at=OLD)
        self._insert("active-old", "active")
        self._insert("expired-old", "expired")
        self._insert("recent-rotated", "rotated", rotated_at=RECENT)
        self._insert("recent-revoked", "revoked", revoked_at=RECENT)
        self._insert("cutoff-rotated", "rotated", rotated_at=CUTOFF)
        self._insert("null-terminal", "rotated")

        result = license_admin.prune_refresh_sessions(
            self.db,
            retention_days=30,
            batch_size=10,
            max_batches=1,
            now=NOW,
        )

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["deleted_rotated"], 1)
        self.assertEqual(result["deleted_revoked"], 1)
        self.assertFalse(result["more_remaining"])
        with sqlite3.connect(self.db) as connection:
            remaining = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT id,status FROM refresh_sessions ORDER BY id"
                )
            }
            audit = connection.execute(
                "SELECT event_type,data_json FROM audit_events "
                "WHERE event_type='refresh_sessions.pruned'"
            ).fetchall()
        self.assertEqual(
            remaining,
            {
                "active-old": "active",
                "cutoff-rotated": "rotated",
                "expired-old": "expired",
                "null-terminal": "rotated",
                "recent-revoked": "revoked",
                "recent-rotated": "rotated",
            },
        )
        self.assertEqual(len(audit), 1)

    def test_batch_and_command_bounds_are_enforced(self) -> None:
        for index in range(12):
            self._insert(f"old-{index:02d}", "rotated", rotated_at=OLD)

        first = license_admin.prune_refresh_sessions(
            self.db,
            retention_days=30,
            batch_size=5,
            max_batches=1,
            now=NOW,
        )
        self.assertEqual(first["deleted"], 5)
        self.assertEqual(first["batches"], 1)
        self.assertTrue(first["more_remaining"])

        second = license_admin.prune_refresh_sessions(
            self.db,
            retention_days=30,
            batch_size=5,
            max_batches=2,
            now=NOW,
        )
        self.assertEqual(second["deleted"], 7)
        self.assertEqual(second["batches"], 2)
        self.assertFalse(second["more_remaining"])

        invalid = (
            {"retention_days": 0},
            {"batch_size": MAX_REFRESH_SESSION_BATCH_SIZE + 1},
            {"max_batches": MAX_REFRESH_SESSION_MAX_BATCHES + 1},
        )
        for override in invalid:
            options = {
                "retention_days": 30,
                "batch_size": 5,
                "max_batches": 1,
                "now": NOW,
            }
            options.update(override)
            with self.subTest(override=override):
                with self.assertRaises(register_user.RegistrationError):
                    license_admin.prune_refresh_sessions(self.db, **options)

    def test_partial_indexes_are_used_for_terminal_cutoff_queries(self) -> None:
        with sqlite3.connect(self.db) as connection:
            rotated_plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM refresh_sessions "
                    "WHERE status='rotated' AND rotated_at IS NOT NULL "
                    "AND rotated_at < ? ORDER BY rotated_at,id LIMIT 10",
                    (CUTOFF,),
                )
            )
            revoked_plan = " ".join(
                str(row[3])
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT id FROM refresh_sessions "
                    "WHERE status='revoked' AND revoked_at IS NOT NULL "
                    "AND revoked_at < ? ORDER BY revoked_at,id LIMIT 10",
                    (CUTOFF,),
                )
            )
        self.assertIn("idx_refresh_sessions_rotated_retention", rotated_plan)
        self.assertIn("idx_refresh_sessions_revoked_retention", revoked_plan)

    def test_concurrent_pruners_preserve_active_rows_devices_and_foreign_keys(self) -> None:
        for index in range(80):
            status = "rotated" if index % 2 == 0 else "revoked"
            self._insert(
                f"terminal-{index:03d}",
                status,
                rotated_at=OLD if status == "rotated" else None,
                revoked_at=OLD if status == "revoked" else None,
            )
        for index in range(3):
            self._insert(f"active-{index}", "active")

        def prune() -> dict:
            return license_admin.prune_refresh_sessions(
                self.db,
                retention_days=30,
                batch_size=7,
                max_batches=10,
                now=NOW,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result() for future in (pool.submit(prune), pool.submit(prune))]

        self.assertEqual(sum(item["deleted"] for item in results), 80)
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            statuses = connection.execute(
                "SELECT status,COUNT(*) FROM refresh_sessions GROUP BY status"
            ).fetchall()
            device = connection.execute(
                "SELECT id,status FROM devices WHERE id='device-1'"
            ).fetchone()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(statuses, [("active", 3)])
        self.assertEqual(device, ("device-1", "active"))
        self.assertEqual(foreign_key_errors, [])

    def test_v2_database_migrates_to_retention_indexes_without_data_loss(self) -> None:
        legacy = Path(self.temp.name) / "v2.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(
                connection,
                applied_at="2026-10-01T00:00:00Z",
                migrations=MIGRATIONS[:2],
            )
            register_user.seed_plans(connection, "2026-10-01T00:00:00Z")
            connection.execute(
                "INSERT INTO customers VALUES("
                "'c','Alice','alice','','active',?,?)",
                (OLD, OLD),
            )
            connection.execute(
                "INSERT INTO entitlements(id,customer_id,plan_id,status,starts_at,"
                "plan_snapshot_json,created_at,updated_at) VALUES("
                "'e','c','free','active',?,'{}',?,?)",
                (OLD, OLD, OLD),
            )
            connection.execute(
                "INSERT INTO devices(id,entitlement_id,install_id,device_hash,status,"
                "activated_at,last_seen_at) VALUES("
                "'d','e','i','h','active',?,?)",
                (OLD, OLD),
            )
            connection.execute(
                "INSERT INTO refresh_sessions(id,device_id,token_hash,status,created_at,"
                "expires_at,rotated_at) VALUES('r','d','t','rotated',?,?,?)",
                (OLD, FUTURE, OLD),
            )
            connection.commit()

        result = register_user.initialize_database(legacy)
        self.assertEqual(result["schema_version"], LATEST_SCHEMA_VERSION)
        with sqlite3.connect(legacy) as connection:
            indexes = {
                row[1]: bool(row[4])
                for row in connection.execute("PRAGMA index_list(refresh_sessions)")
            }
            session = connection.execute(
                "SELECT id,status FROM refresh_sessions"
            ).fetchall()
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertTrue(indexes["idx_refresh_sessions_rotated_retention"])
        self.assertTrue(indexes["idx_refresh_sessions_revoked_retention"])
        self.assertEqual(session, [("r", "rotated")])
        self.assertEqual(versions, [(1,), (2,), (3,)])

    def test_daily_backup_runs_bounded_prune_only_after_verified_backup(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "deploy/el9/backup-database.sh"
        ).read_text(encoding="utf-8")
        integrity = script.index("PRAGMA integrity_check;")
        destination = script.index('mv -- "${TEMPORARY}" "${DESTINATION}"')
        prune = script.index("prune-refresh-sessions")
        self.assertLess(integrity, destination)
        self.assertLess(destination, prune)
        self.assertIn('--batch-size "${SESSION_BATCH_SIZE}"', script)
        self.assertIn('--max-batches "${SESSION_MAX_BATCHES}"', script)


if __name__ == "__main__":
    unittest.main()
