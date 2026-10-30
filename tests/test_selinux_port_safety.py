from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
LIBRARY = ROOT / "deploy" / "el9" / "selinux-port-safety.sh"
COMMAND_SHIM = FIXTURES / "selinux-command-shim.sh"


class SelinuxPortSafetyTests(unittest.TestCase):
    def _run(
        self,
        *,
        mode: str,
        listing: str,
        boolean_state: str = "on",
        list_exit: int = 0,
        tls_mode: str = "http",
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_dir = root / "bin"
            command_dir.mkdir()
            calls = root / "calls.log"
            calls.touch()
            acme_webroot = root / "acme"
            acme_webroot.mkdir()
            for name in (
                "getenforce",
                "semanage",
                "getsebool",
                "setsebool",
                "restorecon",
            ):
                # The release allow-list restores this tracked fixture to 0755,
                # while per-command 0700 copies add a second isolation layer.
                # If an input mode regresses, PATH still cannot fall through to
                # real host SELinux tools during candidate validation.
                command_shim = command_dir / name
                command_shim.write_bytes(COMMAND_SHIM.read_bytes())
                command_shim.chmod(0o700)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{command_dir}{os.pathsep}{environment['PATH']}",
                    "WKM_TEST_CALLS": str(calls),
                    "WKM_TEST_SELINUX_MODE": mode,
                    "WKM_TEST_PORT_LISTING": str(FIXTURES / listing),
                    "WKM_TEST_BOOLEAN_STATE": boolean_state,
                    "WKM_TEST_LIST_EXIT": str(list_exit),
                    "WKM_TEST_PUBLIC_PORT": "9000",
                    "WKM_TEST_ACME_WEBROOT": str(acme_webroot),
                }
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"""
set -Eeuo pipefail
PUBLIC_PORT=9000
TLS_MODE={tls_mode}
ACME_WEBROOT="${{WKM_TEST_ACME_WEBROOT}}"
SELINUX_PORT_ADDED=0
SELINUX_BOOLEAN_CHANGED=0
source {LIBRARY!s}
configure_selinux
printf 'flags=%s,%s\\n' \
  "${{SELINUX_PORT_ADDED}}" "${{SELINUX_BOOLEAN_CHANGED}}"
""",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            recorded_calls = [
                line.rstrip() for line in calls.read_text(encoding="utf-8").splitlines()
            ]
        return result, recorded_calls

    def test_disabled_selinux_skips_every_selinux_mutation_and_query(self) -> None:
        result, calls = self._run(
            mode="Disabled", listing="selinux_ports_foreign.txt"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("flags=0,0", result.stdout)
        self.assertEqual(calls, ["getenforce"])

    def test_existing_http_range_uses_el9_awk_without_mutation(self) -> None:
        result, calls = self._run(
            mode="Enforcing", listing="selinux_ports_http.txt"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("flags=0,0", result.stdout)
        self.assertEqual(
            calls,
            [
                "getenforce",
                "semanage port -l",
                "getsebool httpd_can_network_connect",
            ],
        )
        self.assertNotIn("for (index =", LIBRARY.read_text(encoding="utf-8"))

    def test_foreign_type_fails_closed_before_add_or_boolean_access(self) -> None:
        result, calls = self._run(
            mode="Permissive", listing="selinux_ports_foreign.txt"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unreserved_port_t", result.stderr)
        self.assertEqual(calls, ["getenforce", "semanage port -l"])
        self.assertFalse(any("port -a" in call for call in calls))
        self.assertFalse(any(call.startswith("getsebool") for call in calls))
        self.assertFalse(any(call.startswith("setsebool") for call in calls))

    def test_unassigned_port_is_added_only_after_complete_table_check(self) -> None:
        result, calls = self._run(
            mode="Permissive",
            listing="selinux_ports_unassigned.txt",
            boolean_state="off",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("flags=1,1", result.stdout)
        self.assertEqual(
            calls,
            [
                "getenforce",
                "semanage port -l",
                "semanage port -a -t http_port_t -p tcp 9000",
                "getsebool httpd_can_network_connect",
                "setsebool -P httpd_can_network_connect 1",
            ],
        )

    def test_letsencrypt_mode_labels_existing_acme_webroot_only(self) -> None:
        result, calls = self._run(
            mode="Enforcing",
            listing="selinux_ports_http.txt",
            tls_mode="letsencrypt",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(call.startswith("restorecon -RF ") for call in calls))

    def test_failed_table_read_fails_closed_without_any_write(self) -> None:
        result, calls = self._run(
            mode="Enforcing",
            listing="selinux_ports_unassigned.txt",
            list_exit=1,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法读取 SELinux 端口类型表", result.stderr)
        self.assertEqual(calls, ["getenforce", "semanage port -l"])


if __name__ == "__main__":
    unittest.main()
