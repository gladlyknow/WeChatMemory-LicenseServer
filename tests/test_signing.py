from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from license_server.__main__ import main
from license_server.signing import (
    SigningKeyError,
    TicketSigner,
    inspect_private_key,
    load_public_key_manifest,
    publish_public_key,
    verify_private_key,
    write_new_public_key_manifest,
)


class SigningKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.key = self.root / "signing.pem"
        self.manifest = self.root / "public-keys.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ensure_is_atomic_idempotent_and_never_overwrites(self) -> None:
        first, created = TicketSigner.ensure(self.key, "rotation-1")
        original = self.key.read_bytes()
        second, created_again = TicketSigner.ensure(self.key, "rotation-1")

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(self.key.read_bytes(), original)
        self.assertEqual(first.public_key_b64(), second.public_key_b64())
        if os.name != "nt":
            self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)

    def test_manifest_verification_and_same_id_mismatch_are_fail_closed(self) -> None:
        signer, _created = TicketSigner.ensure(self.key, "rotation-1")
        write_new_public_key_manifest(self.manifest, signer)
        verified = verify_private_key(
            self.key, "rotation-1", self.manifest, require_active=True
        )
        self.assertEqual(verified["public_key"], signer.public_key_b64())
        self.assertEqual(
            inspect_private_key(self.key, "rotation-1")["public_key"],
            signer.public_key_b64(),
        )

        other_path = self.root / "other.pem"
        TicketSigner.ensure(other_path, "rotation-1")
        with self.assertRaisesRegex(SigningKeyError, "does not match"):
            verify_private_key(
                other_path, "rotation-1", self.manifest, require_active=True
            )
        with self.assertRaises(FileExistsError):
            write_new_public_key_manifest(self.manifest, signer)

    def test_manifest_rejects_duplicate_key_ids(self) -> None:
        signer, _created = TicketSigner.ensure(self.key, "rotation-1")
        record = inspect_private_key(self.key, "rotation-1")
        self.manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "algorithm": "Ed25519",
                    "encoding": "base64url-raw-no-padding",
                    "active_key_id": "rotation-1",
                    "keys": [record, record],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SigningKeyError, "duplicate"):
            load_public_key_manifest(self.manifest)

    def test_manifest_rejects_public_key_hash_tampering(self) -> None:
        signer, _created = TicketSigner.ensure(self.key, "rotation-1")
        write_new_public_key_manifest(self.manifest, signer)
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["keys"][0]["raw_sha256"] = "0" * 64
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(SigningKeyError, "hash does not match"):
            load_public_key_manifest(self.manifest)

    def test_publish_requires_explicit_first_activation_and_rotates_atomically(self) -> None:
        first, _created = TicketSigner.ensure(self.key, "rotation-1")
        with self.assertRaisesRegex(SigningKeyError, "needs --activate"):
            publish_public_key(self.manifest, first)
        manifest, changed = publish_public_key(self.manifest, first, activate=True)
        self.assertTrue(changed)
        self.assertEqual(manifest["active_key_id"], "rotation-1")

        second_path = self.root / "second.pem"
        second, _created = TicketSigner.ensure(second_path, "rotation-2")
        manifest, changed = publish_public_key(self.manifest, second)
        self.assertTrue(changed)
        self.assertEqual(manifest["active_key_id"], "rotation-1")
        self.assertEqual(manifest["keys"][1]["status"], "retiring")

        manifest, changed = publish_public_key(self.manifest, second, activate=True)
        self.assertTrue(changed)
        self.assertEqual(manifest["active_key_id"], "rotation-2")
        statuses = {item["key_id"]: item["status"] for item in manifest["keys"]}
        self.assertEqual(statuses, {"rotation-1": "retiring", "rotation-2": "active"})

        _manifest, changed = publish_public_key(self.manifest, second, activate=True)
        self.assertFalse(changed)

    def test_publish_refuses_same_key_id_with_different_public_key(self) -> None:
        first, _created = TicketSigner.ensure(self.key, "rotation-1")
        publish_public_key(self.manifest, first, activate=True)
        other_path = self.root / "other.pem"
        other, _created = TicketSigner.ensure(other_path, "rotation-1")
        before = self.manifest.read_bytes()
        with self.assertRaisesRegex(SigningKeyError, "already bound"):
            publish_public_key(self.manifest, other)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_ensure_cli_closes_key_and_manifest_loop_idempotently(self) -> None:
        arguments = [
            "signing-key",
            "ensure",
            "--path",
            str(self.key),
            "--key-id",
            "rotation-1",
            "--manifest",
            str(self.manifest),
            "--publish",
            "--activate",
        ]
        with redirect_stdout(StringIO()):
            self.assertEqual(main(arguments), 0)
        key_before = self.key.read_bytes()
        manifest_before = self.manifest.read_bytes()
        with redirect_stdout(StringIO()):
            self.assertEqual(main(arguments), 0)
        self.assertEqual(self.key.read_bytes(), key_before)
        self.assertEqual(self.manifest.read_bytes(), manifest_before)

    def test_ensure_cli_without_publish_never_regenerates_manifested_key(self) -> None:
        signer, _created = TicketSigner.ensure(self.key, "rotation-1")
        publish_public_key(self.manifest, signer, activate=True)
        self.key.unlink()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = main(
                [
                    "signing-key",
                    "ensure",
                    "--path",
                    str(self.key),
                    "--key-id",
                    "rotation-1",
                    "--manifest",
                    str(self.manifest),
                ]
            )
        self.assertEqual(result, 2)
        self.assertFalse(self.key.exists())

        new_key = self.root / "new-key.pem"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = main(
                [
                    "signing-key",
                    "ensure",
                    "--path",
                    str(new_key),
                    "--key-id",
                    "rotation-2",
                    "--manifest",
                    str(self.manifest),
                ]
            )
        self.assertEqual(result, 0)
        self.assertTrue(new_key.exists())
        self.assertNotIn(
            "rotation-2", self.manifest.read_text(encoding="utf-8")
        )

    def test_tracked_manifest_is_valid_and_matches_client_key_id(self) -> None:
        tracked = load_public_key_manifest(
            Path(__file__).resolve().parents[1]
            / "config"
            / "signing-public-keys.json"
        )
        self.assertEqual(tracked["active_key_id"], "2026-10-30")
        self.assertEqual(tracked["algorithm"], "Ed25519")
        self.assertEqual(tracked["encoding"], "base64url-raw-no-padding")
        self.assertEqual(len(tracked["keys"]), 1)


if __name__ == "__main__":
    unittest.main()
