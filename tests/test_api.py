from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from license_server.app import create_app
from license_server.service import LicenseError, token_hash
from license_server.signing import TicketSigner
from license_admin import (
    reset_activation_code,
    set_device_status,
    set_entitlement_status,
    set_plan,
)
from register_user import initialize_database, register_user


class LicenseApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "license.sqlite3"
        initialize_database(self.database)
        self.registered = register_user(
            self.database, "commercial-user", "", "permanent_personal", None
        )
        self.signer = TicketSigner(Ed25519PrivateKey.generate(), "test-2026-01")
        self.app = create_app(database_path=self.database, signer=self.signer)
        self.app.testing = True
        self.client = self.app.test_client()
        self.install_id = "i" * 64
        self.device_hash = "a" * 64

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, **extra):
        value = {
            "account": "commercial-user",
            "activation_code": self.registered["activation_code"],
            "install_id": self.install_id,
            "device_hash": self.device_hash,
            "platform": "windows-amd64",
            "app_version": "0.3.2",
        }
        value.update(extra)
        return value

    def reset_code(self, *, keep_sessions: bool = False) -> str:
        result = reset_activation_code(
            self.database, "commercial-user", keep_sessions=keep_sessions
        )
        self.registered["activation_code"] = result["activation_code"]
        return result["activation_code"]

    def test_health_ready_and_public_key(self) -> None:
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/readyz").status_code, 200)
        manifest = self.client.get("/v1/keys").get_json()
        self.assertEqual(manifest["algorithm"], "Ed25519")
        self.assertEqual(manifest["encoding"], "base64url-raw-no-padding")
        keys = manifest["keys"]
        self.assertEqual(keys[0]["key_id"], "test-2026-01")
        self.assertEqual(len(keys[0]["raw_sha256"]), 64)
        self.assertEqual(len(keys[0]["spki_sha256"]), 64)

    def test_numeric_environment_validation_is_fail_closed(self) -> None:
        invalid_cases = (
            {"REFRESH_TOKEN_DAYS": "0"},
            {"LICENSE_TICKET_HOURS": "not-an-integer"},
            {"RATE_LIMIT_PER_MINUTE": "0"},
            {"RATE_LIMIT_GLOBAL_PER_MINUTE": "1000001"},
            {"RATE_LIMIT_MAX_KEYS": "100001"},
            {
                "RATE_LIMIT_PER_MINUTE": "21",
                "RATE_LIMIT_GLOBAL_PER_MINUTE": "20",
            },
        )
        for values in invalid_cases:
            with self.subTest(values=values), mock.patch.dict(
                os.environ, values, clear=False
            ):
                with self.assertRaises(RuntimeError):
                    create_app(database_path=self.database, signer=self.signer)

    def test_public_readiness_is_shallow_rate_limited_and_deep_check_is_local(self) -> None:
        database = self.app.extensions["license_database"]
        with mock.patch.object(database, "ready", return_value=True) as shallow:
            with mock.patch.object(database, "deep_ready", return_value=True) as deep:
                self.assertEqual(self.client.get("/readyz").status_code, 200)
                shallow.assert_called_once_with()
                deep.assert_not_called()

                blocked = self.client.get(
                    "/readyz/deep", headers={"X-Forwarded-For": "203.0.113.8"}
                )
                self.assertEqual(blocked.status_code, 404)
                deep.assert_not_called()

                blocked_direct = self.client.get(
                    "/readyz/deep", environ_base={"REMOTE_ADDR": "198.51.100.9"}
                )
                self.assertEqual(blocked_direct.status_code, 404)
                deep.assert_not_called()

                local = self.client.get("/readyz/deep")
                self.assertEqual(local.status_code, 200)
                self.assertEqual(local.get_json()["check"], "deep")
                deep.assert_called_once_with()

        limiter = self.app.extensions["rate_limiter"]
        limiter.limit = 1
        # The first public readiness call above consumed the only per-IP slot.
        self.assertEqual(self.client.get("/readyz").status_code, 429)
        # Liveness remains a cheap, dependency-free probe.
        self.assertEqual(self.client.get("/healthz").status_code, 200)

        nginx = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "el9"
            / "nginx-production.conf"
        ).read_text(encoding="utf-8")
        self.assertIn("location = /readyz/deep", nginx)
        self.assertRegex(nginx, r"location = /readyz/deep\s*\{\s*return 404;")

    def test_public_time_and_keys_are_limited_but_local_probes_are_exempt(self) -> None:
        limiter = self.app.extensions["rate_limiter"]
        limiter.limit = 1

        time_headers = {"X-Forwarded-For": "203.0.113.10"}
        self.assertEqual(
            self.client.get("/v1/time", headers=time_headers).status_code, 200
        )
        limited_time = self.client.get("/v1/time", headers=time_headers)
        self.assertEqual(limited_time.status_code, 429)
        self.assertEqual(limited_time.get_json(), {"error": "rate_limited"})

        key_headers = {"X-Forwarded-For": "203.0.113.11"}
        self.assertEqual(
            self.client.get("/v1/keys", headers=key_headers).status_code, 200
        )
        limited_keys = self.client.get("/v1/keys", headers=key_headers)
        self.assertEqual(limited_keys.status_code, 429)
        self.assertEqual(limited_keys.get_json(), {"error": "rate_limited"})

        # Liveness and the direct-loopback deep probe remain available even
        # after the default loopback source has exhausted its public API slot.
        self.assertEqual(self.client.get("/readyz").status_code, 200)
        self.assertEqual(self.client.get("/readyz").status_code, 429)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/readyz/deep").status_code, 200)

    def test_readiness_fails_closed_when_v2_minimum_schema_is_incomplete(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("DROP INDEX idx_refresh_sessions_device")
            connection.commit()

        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/readyz").status_code, 503)
        deep = self.client.get("/readyz/deep")
        self.assertEqual(deep.status_code, 503)
        self.assertEqual(deep.get_json()["check"], "deep")

    def test_activation_returns_signed_ticket_and_rotating_refresh_token(self) -> None:
        activated = self.client.post(
            "/v1/activate",
            json=self.payload(
                account="COMMERCIAL-USER",
                device_hash=self.device_hash.upper(),
            ),
        )
        self.assertEqual(activated.status_code, 200, activated.get_data(as_text=True))
        body = activated.get_json()
        self.assertTrue(body["refresh_token"].startswith("wkmr_"))
        ticket = body["ticket"]
        signature = ticket.pop("signature")
        canonical = json.dumps(
            ticket, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        padded = signature + "=" * (-len(signature) % 4)
        self.signer.private_key.public_key().verify(
            base64.urlsafe_b64decode(padded), canonical
        )
        self.assertEqual(ticket["account"], "commercial-user")
        self.assertEqual(ticket["install_id"], self.install_id)
        self.assertEqual(ticket["device_hash"], self.device_hash)
        self.assertIn("realtime_sync", ticket["capabilities"])
        with sqlite3.connect(self.database) as connection:
            stored_device_hash = connection.execute(
                "SELECT device_hash FROM devices WHERE id=?",
                (ticket["device_id"],),
            ).fetchone()[0]
        self.assertEqual(ticket["device_hash"], stored_device_hash)

        wrong_device = self.client.post(
            "/v1/licenses/refresh",
            json={
                "refresh_token": body["refresh_token"],
                "install_id": self.install_id,
                "device_hash": "b" * 64,
            },
        )
        self.assertEqual(wrong_device.status_code, 401)

        refreshed = self.client.post(
            "/v1/licenses/refresh",
            json={
                "refresh_token": body["refresh_token"],
                "install_id": self.install_id,
                "device_hash": self.device_hash.upper(),
                "platform": "windows-amd64",
                "app_version": "0.3.2",
            },
        )
        self.assertEqual(refreshed.status_code, 200)
        refreshed_body = refreshed.get_json()
        new_token = refreshed_body["refresh_token"]
        self.assertNotEqual(new_token, body["refresh_token"])
        self.assertEqual(refreshed_body["ticket"]["device_hash"], stored_device_hash)

        status = self.client.get(
            "/v1/licenses/status",
            headers={"Authorization": f"Bearer {new_token}"},
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["ticket"]["device_hash"], stored_device_hash)
        replay = self.client.post(
            "/v1/licenses/refresh",
            json={
                "refresh_token": body["refresh_token"],
                "install_id": self.install_id,
                "device_hash": self.device_hash,
            },
        )
        self.assertEqual(replay.status_code, 401)

    def test_activation_credential_is_consumed_once_and_audited(self) -> None:
        first = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(first.status_code, 200)

        replay = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(replay.get_json(), {"error": "activation_failed"})

        with sqlite3.connect(self.database) as connection:
            credential = connection.execute(
                "SELECT status,last_used_at,revoked_at FROM activation_credentials"
            ).fetchone()
            event_count = connection.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE event_type='activation_credential.consumed'"
            ).fetchone()[0]
        self.assertEqual(credential[0], "revoked")
        self.assertIsNotNone(credential[1])
        self.assertEqual(credential[1], credential[2])
        self.assertEqual(event_count, 1)

    def test_legacy_used_but_active_credential_fails_closed(self) -> None:
        # Releases through 0.2.5 recorded last_used_at without changing
        # status.  An upgraded server must not grant that historical code one
        # additional successful replay before the new one-time CAS applies.
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE activation_credentials SET status='active',last_used_at=?",
                ("2026-10-29T00:00:00Z",),
            )

        result = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.get_json(), {"error": "activation_failed"})
        with sqlite3.connect(self.database) as connection:
            credential = connection.execute(
                "SELECT status,last_used_at FROM activation_credentials"
            ).fetchone()
            devices = connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            sessions = connection.execute(
                "SELECT COUNT(*) FROM refresh_sessions"
            ).fetchone()[0]
        self.assertEqual(credential, ("active", "2026-10-29T00:00:00Z"))
        self.assertEqual((devices, sessions), (0, 0))

    def test_concurrent_activation_consumes_credential_atomically(self) -> None:
        manager = self.app.extensions["license_manager"]
        barrier = threading.Barrier(2)

        def activate_once() -> str:
            barrier.wait(timeout=5)
            try:
                manager.activate(self.payload())
            except LicenseError as error:
                return error.public_code
            return "ok"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: activate_once(), range(2)))
        self.assertEqual(sorted(results), ["activation_failed", "ok"])

        with sqlite3.connect(self.database) as connection:
            devices = connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            sessions = connection.execute(
                "SELECT COUNT(*) FROM refresh_sessions WHERE status='active'"
            ).fetchone()[0]
            consumed_events = connection.execute(
                "SELECT COUNT(*) FROM audit_events "
                "WHERE event_type='activation_credential.consumed'"
            ).fetchone()[0]
        self.assertEqual((devices, sessions, consumed_events), (1, 1, 1))

    def test_activation_failure_does_not_reveal_username_existence(self) -> None:
        wrong_code = self.client.post(
            "/v1/activate", json=self.payload(activation_code="WKM1-WRONG-WRONG")
        )
        wrong_user = self.client.post(
            "/v1/activate", json=self.payload(account="missing-user")
        )
        self.assertEqual(wrong_code.status_code, 401)
        self.assertEqual(wrong_user.status_code, 401)
        self.assertEqual(wrong_code.get_json(), wrong_user.get_json())

    def test_client_metadata_rejects_terminal_controls_and_non_strings(self) -> None:
        hostile_values = (
            ("platform", "windows\x1b]52;c;ZXZpbA==\x07"),
            ("app_version", "0.3.5\nforged"),
            ("platform", "windows\x9b31m"),
            ("platform", "windows\u202eevil"),
            ("app_version", {"unexpected": "mapping"}),
        )
        for key, value in hostile_values:
            with self.subTest(stage="activate", key=key, value=value):
                payload = self.payload()
                payload[key] = value
                response = self.client.post("/v1/activate", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"error": "invalid_request"})

        with sqlite3.connect(self.database) as connection:
            credential = connection.execute(
                "SELECT status,last_used_at FROM activation_credentials"
            ).fetchone()
            devices = connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            sessions = connection.execute(
                "SELECT COUNT(*) FROM refresh_sessions"
            ).fetchone()[0]
        self.assertEqual(credential, ("active", None))
        self.assertEqual((devices, sessions), (0, 0))

        activated = self.client.post("/v1/activate", json=self.payload()).get_json()
        token = activated["refresh_token"]
        for key, value in hostile_values:
            with self.subTest(stage="refresh", key=key, value=value):
                payload = {
                    "refresh_token": token,
                    "install_id": self.install_id,
                    "device_hash": self.device_hash,
                    key: value,
                }
                response = self.client.post("/v1/licenses/refresh", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {"error": "invalid_request"})

        status = self.client.get(
            "/v1/licenses/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status.status_code, 200)

    def test_deactivate_revokes_refresh_session(self) -> None:
        body = self.client.post("/v1/activate", json=self.payload()).get_json()
        result = self.client.post(
            "/v1/devices/deactivate", json={"refresh_token": body["refresh_token"]}
        )
        self.assertEqual(result.status_code, 200)
        status = self.client.get(
            "/v1/licenses/status",
            headers={"Authorization": f"Bearer {body['refresh_token']}"},
        )
        self.assertEqual(status.status_code, 401)

    def test_deactivated_device_cannot_bypass_free_device_limit(self) -> None:
        registered = register_user(
            self.database, "free-user", "", "free", None
        )

        def free_payload(marker: str) -> dict[str, str]:
            return {
                "account": "free-user",
                "activation_code": registered["activation_code"],
                "install_id": marker * 64,
                "device_hash": marker * 64,
                "platform": "windows-amd64",
                "app_version": "0.3.3",
            }

        first = self.client.post("/v1/activate", json=free_payload("a"))
        self.assertEqual(first.status_code, 200)
        deactivated = self.client.post(
            "/v1/devices/deactivate",
            json={"refresh_token": first.get_json()["refresh_token"]},
        )
        self.assertEqual(deactivated.status_code, 200)
        registered["activation_code"] = reset_activation_code(
            self.database, "free-user"
        )["activation_code"]
        second = self.client.post("/v1/activate", json=free_payload("b"))
        self.assertEqual(second.status_code, 200)

        registered["activation_code"] = reset_activation_code(
            self.database, "free-user"
        )["activation_code"]
        blocked = self.client.post("/v1/activate", json=free_payload("a"))
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json(), {"error": "device_limit_reached"})
        with sqlite3.connect(self.database) as connection:
            rows = connection.execute(
                "SELECT install_id,status FROM devices WHERE entitlement_id="
                "(SELECT id FROM entitlements WHERE customer_id="
                "(SELECT id FROM customers WHERE username_norm='free-user')) "
                "ORDER BY install_id"
            ).fetchall()
        self.assertEqual(rows, [("a" * 64, "deactivated"), ("b" * 64, "active")])

    def test_blocked_device_cannot_reactivate_with_activation_code(self) -> None:
        activated = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(activated.status_code, 200)
        device_id = activated.get_json()["ticket"]["device_id"]
        set_device_status(
            self.database, "commercial-user", device_id, "blocked"
        )

        self.reset_code()
        blocked = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(blocked.status_code, 401)
        self.assertEqual(blocked.get_json(), {"error": "activation_failed"})
        with sqlite3.connect(self.database) as connection:
            status = connection.execute(
                "SELECT status FROM devices WHERE id=?", (device_id,)
            ).fetchone()[0]
            active_sessions = connection.execute(
                "SELECT COUNT(*) FROM refresh_sessions "
                "WHERE device_id=? AND status='active'",
                (device_id,),
            ).fetchone()[0]
        self.assertEqual(status, "blocked")
        self.assertEqual(active_sessions, 0)

    def test_deactivated_device_can_reactivate_when_capacity_is_available(self) -> None:
        activated = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(activated.status_code, 200)
        deactivated = self.client.post(
            "/v1/devices/deactivate",
            json={"refresh_token": activated.get_json()["refresh_token"]},
        )
        self.assertEqual(deactivated.status_code, 200)

        self.reset_code()
        reactivated = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(reactivated.status_code, 200)
        self.assertEqual(
            reactivated.get_json()["ticket"]["device_id"],
            activated.get_json()["ticket"]["device_id"],
        )

    def test_status_rejects_expired_session_and_inactive_device(self) -> None:
        body = self.client.post("/v1/activate", json=self.payload()).get_json()
        refresh_digest = token_hash("refresh", body["refresh_token"])
        expired = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE refresh_sessions SET expires_at=? WHERE token_hash=?",
                (expired, refresh_digest),
            )
        response = self.client.get(
            "/v1/licenses/status",
            headers={"Authorization": f"Bearer {body['refresh_token']}"},
        )
        self.assertEqual(response.status_code, 401)

        self.reset_code()
        second = self.client.post("/v1/activate", json=self.payload()).get_json()
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE devices SET status='blocked'")
        response = self.client.get(
            "/v1/licenses/status",
            headers={"Authorization": f"Bearer {second['refresh_token']}"},
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_changes_are_visible_on_the_next_client_refresh(self) -> None:
        activated = self.client.post("/v1/activate", json=self.payload()).get_json()
        set_plan(
            self.database,
            "commercial-user",
            "pro_subscription",
            30,
            device_limit=4,
            offline_days=10,
        )
        upgraded = self.client.post(
            "/v1/licenses/refresh",
            json={
                "refresh_token": activated["refresh_token"],
                "install_id": self.install_id,
                "device_hash": self.device_hash,
                "platform": "windows-amd64",
                "app_version": "0.3.2",
            },
        ).get_json()
        self.assertEqual(upgraded["ticket"]["plan"], "pro_subscription")
        self.assertIn("standard_analytics", upgraded["ticket"]["capabilities"])
        self.assertIn("advanced_analytics", upgraded["ticket"]["capabilities"])

        set_entitlement_status(self.database, "commercial-user", "suspended")
        read_only = self.client.post(
            "/v1/licenses/refresh",
            json={
                "refresh_token": upgraded["refresh_token"],
                "install_id": self.install_id,
                "device_hash": self.device_hash,
                "platform": "windows-amd64",
                "app_version": "0.3.2",
            },
        )
        self.assertEqual(read_only.status_code, 200)
        self.assertEqual(read_only.get_json()["ticket"]["capabilities"], ["read_only"])

    def test_legacy_pro_snapshot_matches_current_client_analytics_contract(self) -> None:
        set_plan(
            self.database,
            "commercial-user",
            "pro_subscription",
            30,
        )
        with sqlite3.connect(self.database) as connection:
            raw = connection.execute(
                "SELECT plan_snapshot_json FROM entitlements ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
            snapshot = json.loads(raw)
            snapshot["features"] = [
                capability
                for capability in snapshot["features"]
                if capability != "standard_analytics"
            ]
            self.assertIn("advanced_analytics", snapshot["features"])
            connection.execute(
                "UPDATE entitlements SET plan_snapshot_json=?",
                (json.dumps(snapshot, sort_keys=True, separators=(",", ":")),),
            )

        response = self.client.post("/v1/activate", json=self.payload())
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        capabilities = response.get_json()["ticket"]["capabilities"]
        self.assertIn("standard_analytics", capabilities)
        self.assertIn("advanced_analytics", capabilities)


if __name__ == "__main__":
    unittest.main()
