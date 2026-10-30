from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "deploy" / "el9" / "public-ip-safety.sh"
COMMAND_SHIM = ROOT / "tests" / "fixtures" / "ip-command-shim.sh"


class PublicIpSafetyTests(unittest.TestCase):
    def _run(
        self,
        *,
        public_ip: str,
        output: str,
        allow_unbound: bool = False,
        command_exit: int = 0,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_dir = root / "bin"
            command_dir.mkdir()
            calls = root / "calls.log"
            calls.touch()
            (command_dir / "ip").symlink_to(COMMAND_SHIM)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{command_dir}{os.pathsep}{environment['PATH']}",
                    "WKM_TEST_CALLS": str(calls),
                    "WKM_TEST_IP_OUTPUT": output,
                    "WKM_TEST_IP_EXIT": str(command_exit),
                }
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; wkm_assert_public_ip_bound "$2" "$3"',
                    "bash",
                    str(LIBRARY),
                    public_ip,
                    "1" if allow_unbound else "0",
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            recorded_calls = calls.read_text(encoding="utf-8").splitlines()
        return result, recorded_calls

    def test_exact_scope_global_address_matches(self) -> None:
        result, calls = self._run(
            public_ip="104.244.90.172",
            output=(
                "2: eth0    inet 104.244.90.172/22 brd 104.244.91.255 "
                "scope global eth0"
            ),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["ip -o -4 addr show scope global"])

    def test_one_digit_mismatch_is_rejected(self) -> None:
        result, calls = self._run(
            public_ip="104.224.90.172",
            output=(
                "2: eth0    inet 104.244.90.172/22 brd 104.244.91.255 "
                "scope global eth0"
            ),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("104.224.90.172", result.stderr)
        self.assertIn("104.244.90.172", result.stderr)
        self.assertEqual(calls, ["ip -o -4 addr show scope global"])

    def test_ip_command_failure_fails_closed(self) -> None:
        result, calls = self._run(
            public_ip="104.244.90.172",
            output="simulated ip failure",
            command_exit=23,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("读取本机 scope global IPv4 失败", result.stderr)
        self.assertEqual(calls, ["ip -o -4 addr show scope global"])

    def test_explicit_nat_override_warns_and_skips_ip_command(self) -> None:
        result, calls = self._run(
            public_ip="203.0.113.9",
            output="",
            allow_unbound=True,
            command_exit=23,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("严重警告", result.stderr)
        self.assertIn("--allow-unbound-public-ip", result.stderr)
        self.assertEqual(calls, [])

    def test_entrypoints_wire_gate_before_host_mutations(self) -> None:
        one_click = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        wrapper = (ROOT / "deploy/el9/deploy-from-data.sh").read_text(
            encoding="utf-8"
        )

        gate = one_click.index(
            'wkm_assert_public_ip_bound "${PUBLIC_IP}" "${ALLOW_UNBOUND_PUBLIC_IP}"'
        )
        for mutation in (
            "dnf install -y",
            "\nconfigure_firewall\n",
            '"${CERTBOT_ROOT}/bin/certbot" certonly',
        ):
            self.assertLess(gate, one_click.index(mutation))
        self.assertIn("--allow-unbound-public-ip)", one_click)
        self.assertIn("--allow-unbound-public-ip)", wrapper)
        self.assertIn("arguments+=(--allow-unbound-public-ip)", wrapper)


if __name__ == "__main__":
    unittest.main()
