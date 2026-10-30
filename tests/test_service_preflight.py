from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from license_server.signing import TicketSigner, write_new_public_key_manifest
from register_user import initialize_database
from scripts.service_preflight import (
    ALLOWED_ENVIRONMENT,
    EXPECTED_PUBLIC_MANIFEST_PATH,
    load_service_environment,
    main,
    run_preflight,
)


class ServicePreflightTests(unittest.TestCase):
    def test_exact_environment_loads_key_manifest_and_ready_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "license.sqlite3"
            key = root / "signing.pem"
            manifest = root / "keys.json"
            environment = root / "service.env"
            initialize_database(database)
            signer = TicketSigner.generate(key, "test-key")
            write_new_public_key_manifest(manifest, signer)
            values = {
                "DATABASE_PATH": str(database),
                "LICENSE_ISSUER": "wechat-memory-license",
                "LICENSE_AUDIENCE": "wechat-memory-client",
                "REFRESH_TOKEN_DAYS": "365",
                "LICENSE_TICKET_HOURS": "24",
                "RATE_LIMIT_PER_MINUTE": "20",
                "RATE_LIMIT_GLOBAL_PER_MINUTE": "1000",
                "RATE_LIMIT_MAX_KEYS": "4096",
                "SIGNING_KEY_ID": "test-key",
                "SIGNING_PRIVATE_KEY_PATH": str(key),
                "SIGNING_PUBLIC_KEYS_PATH": str(EXPECTED_PUBLIC_MANIFEST_PATH),
            }
            self.assertEqual(set(values), set(ALLOWED_ENVIRONMENT))
            environment.write_text(
                "".join(f"{name}={value}\n" for name, value in values.items()),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                run_preflight(environment, manifest)

            environment.write_text(
                "".join(
                    f"{name}={value}\n"
                    for name, value in {
                        **values,
                        "SIGNING_PUBLIC_KEYS_PATH": str(root / "wrong.json"),
                    }.items()
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "service contract"):
                run_preflight(environment, manifest)

    def test_environment_rejects_unknown_duplicate_and_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "service.env"
            path.write_text("UNEXPECTED=value\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected"):
                load_service_environment(path)

            path.write_text("DATABASE_PATH=a\nDATABASE_PATH=b\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_service_environment(path)

            path.write_text("DATABASE_PATH=a\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                load_service_environment(path)

    def test_cli_never_echoes_arbitrary_exception_message(self) -> None:
        stderr = io.StringIO()
        with mock.patch(
            "scripts.service_preflight.run_preflight",
            side_effect=RuntimeError("WKM1-secret wkmr_secret PRIVATE KEY"),
        ), contextlib.redirect_stderr(stderr):
            result = main(["--env", "unused", "--manifest", "unused"])
        self.assertEqual(result, 1)
        self.assertEqual(
            stderr.getvalue(),
            "service preflight failed: runtime_preflight_failed\n",
        )


if __name__ == "__main__":
    unittest.main()
