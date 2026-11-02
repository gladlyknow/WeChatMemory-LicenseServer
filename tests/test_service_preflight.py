from __future__ import annotations

import contextlib
import importlib
import importlib.metadata
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from license_server.signing import TicketSigner, write_new_public_key_manifest
from register_user import initialize_database
from scripts.service_preflight import (
    ALLOWED_ENVIRONMENT,
    RuntimeDependencyError,
    load_service_environment,
    main,
    run_preflight,
    validate_wsgi_runtime,
)


class ServicePreflightTests(unittest.TestCase):
    def test_wsgi_runtime_imports_and_verifies_installed_waitress_files(self) -> None:
        validate_wsgi_runtime()

    def test_wsgi_runtime_rejects_a_required_module_outside_current_venv(self) -> None:
        compat = importlib.import_module("waitress.compat")
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "compat.py"
            outside.write_text("# not the installed dependency\n", encoding="utf-8")
            with mock.patch.object(compat, "__file__", str(outside)):
                with self.assertRaisesRegex(
                    RuntimeDependencyError, "^runtime dependency validation failed$"
                ):
                    validate_wsgi_runtime()

    def test_wsgi_runtime_import_failure_is_sanitized(self) -> None:
        original = importlib.import_module

        def import_with_missing_compat(name: str, package: str | None = None):
            if name == "waitress.compat":
                raise ModuleNotFoundError("secret candidate path")
            return original(name, package)

        with mock.patch(
            "scripts.service_preflight.importlib.import_module",
            side_effect=import_with_missing_compat,
        ):
            with self.assertRaisesRegex(
                RuntimeDependencyError, "^runtime dependency validation failed$"
            ) as raised:
                validate_wsgi_runtime()
        self.assertNotIn("secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_wsgi_runtime_rejects_record_hash_mismatch(self) -> None:
        with mock.patch(
            "scripts.service_preflight._sha256_record_value",
            return_value="not-the-recorded-hash",
        ):
            with self.assertRaisesRegex(
                RuntimeDependencyError, "^runtime dependency validation failed$"
            ):
                validate_wsgi_runtime()

    def test_wsgi_runtime_rejects_deleted_compat_despite_persisted_record(self) -> None:
        installed = importlib.metadata.distribution("waitress")
        record = installed.read_text("RECORD")
        self.assertIsNotNone(record)
        self.assertIn("waitress/compat.py,sha256=", record)

        missing = Path(sys.prefix) / "__deleted_waitress_compat__.py"
        self.assertFalse(missing.exists())
        candidate = mock.Mock(wraps=installed)
        candidate.read_text.return_value = record

        def locate_file(relative_name: str):
            if str(relative_name) == "waitress/compat.py":
                return missing
            return installed.locate_file(relative_name)

        candidate.locate_file.side_effect = locate_file
        with mock.patch(
            "scripts.service_preflight.importlib.metadata.distribution",
            return_value=candidate,
        ):
            with self.assertRaisesRegex(
                RuntimeDependencyError, "^runtime dependency validation failed$"
            ):
                validate_wsgi_runtime()

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
                "SIGNING_PUBLIC_KEYS_PATH": str(manifest),
            }
            self.assertEqual(set(values), set(ALLOWED_ENVIRONMENT))
            environment.write_text(
                "".join(f"{name}={value}\n" for name, value in values.items()),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                run_preflight(environment, manifest)

            wrong_manifest = root / "wrong.json"
            wrong_manifest.write_text("{}\n", encoding="utf-8")
            environment.write_text(
                "".join(
                    f"{name}={value}\n"
                    for name, value in {
                        **values,
                        "SIGNING_PUBLIC_KEYS_PATH": str(wrong_manifest),
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
