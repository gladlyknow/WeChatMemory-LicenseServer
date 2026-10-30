from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "deploy" / "el9" / "signing-key-safety.sh"


def run_gate(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; shift; {script}', "bash", str(LIBRARY), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


class SigningKeyFileSafetyTests(unittest.TestCase):
    def test_exact_metadata_gate_accepts_plain_0600_file_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "signing.pem"
            secret = "DO-NOT-PRINT-PRIVATE-KEY-BYTES"
            key.write_text(secret, encoding="utf-8")
            key.chmod(0o600)
            result = run_gate(
                'wkm_assert_private_key_file "$1" "$2" "$3" 600 test-key',
                str(key),
                str(os.getuid()),
                str(os.getgid()),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_symlink_and_wrong_mode_fail_without_exposing_key_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = root / "signing.pem"
            secret = "DO-NOT-PRINT-PRIVATE-KEY-BYTES"
            key.write_text(secret, encoding="utf-8")
            key.chmod(0o640)
            result = run_gate(
                'wkm_assert_private_key_file "$1" "$2" "$3" 600 test-key',
                str(key),
                str(os.getuid()),
                str(os.getgid()),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(secret, result.stdout + result.stderr)

            link = root / "link.pem"
            link.symlink_to(key)
            result = run_gate(
                'wkm_assert_private_key_file "$1" "$2" "$3" 640 test-key',
                str(link),
                str(os.getuid()),
                str(os.getgid()),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_staged_and_installed_wrappers_have_distinct_metadata_contracts(self) -> None:
        text = LIBRARY.read_text(encoding="utf-8")
        self.assertIn('wkm_assert_private_key_file "${1:-}" 0 0 600', text)
        self.assertIn(
            'wkm_assert_private_key_file "${path}" 0 "${group_id}" 640', text
        )


if __name__ == "__main__":
    unittest.main()
