from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from license_server.app import create_app
from license_server.service import token_hash
from license_server.signing import TicketSigner
from license_admin import set_entitlement_status, set_plan
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

    def test_activation_returns_signed_ticket_and_rotating_refresh_token(self) -> None:
        activated = self.client.post(
            "/v1/activate", json=self.payload(account="COMMERCIAL-USER")
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
        self.assertIn("realtime_sync", ticket["capabilities"])

        refreshed = self.client.post(
            "/v1/licenses/refresh",
            json={
                "refresh_token": body["refresh_token"],
                "install_id": self.install_id,
                "device_hash": self.device_hash,
                "platform": "windows-amd64",
                "app_version": "0.3.2",
            },
        )
        self.assertEqual(refreshed.status_code, 200)
        new_token = refreshed.get_json()["refresh_token"]
        self.assertNotEqual(new_token, body["refresh_token"])
        replay = self.client.post(
            "/v1/licenses/refresh",
            json={
                "refresh_token": body["refresh_token"],
                "install_id": self.install_id,
                "device_hash": self.device_hash,
            },
        )
        self.assertEqual(replay.status_code, 401)

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


if __name__ == "__main__":
    unittest.main()
