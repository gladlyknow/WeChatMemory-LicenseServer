from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "deploy" / "el9" / "shared-host-safety.sh"


def run_bash(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "$1"; shift; {script}', "bash", str(LIBRARY), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


class SharedHostSafetyTests(unittest.TestCase):
    def test_existing_unrelated_site_is_accepted_and_never_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            foreign = conf / "entityecho.conf"
            original = (
                "server { listen 80; server_name entity-echo.com 104.244.90.172; }\n"
            )
            foreign.write_text(original, encoding="utf-8")
            owned = conf / "wechat-memory-license.conf"

            result = run_bash(
                'wkm_assert_no_foreign_nginx_conflict "$1" "$2" "$3" "$4"',
                str(root),
                str(owned),
                "104.224.90.172",
                "9000",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(foreign.read_text(encoding="utf-8"), original)

    def test_exact_ip_or_https_port_collision_fails_closed(self) -> None:
        for body in (
            "server { listen 80; server_name 104.224.90.172; }\n",
            "server { listen 80; server_name\n  104.224.90.172; }\n",
            "server { listen 9000 ssl; server_name another.example; }\n",
            "server { listen [::]:9000 ssl; server_name another.example; }\n",
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                conf = root / "conf.d"
                conf.mkdir()
                (conf / "foreign.conf").write_text(body, encoding="utf-8")
                result = run_bash(
                    'wkm_assert_no_foreign_nginx_conflict "$1" "$2" "$3" "$4"',
                    str(root),
                    str(conf / "wechat-memory-license.conf"),
                    "104.224.90.172",
                    "9000",
                )
                self.assertNotEqual(result.returncode, 0)

    def test_owned_path_rejects_foreign_file_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "foreign.conf"
            target.write_text("server { listen 80; }\n", encoding="utf-8")
            result = run_bash('wkm_assert_owned_nginx_file "$1"', str(target))
            self.assertNotEqual(result.returncode, 0)

            link = root / "owned.conf"
            link.symlink_to(target)
            result = run_bash('wkm_assert_owned_nginx_file "$1"', str(link))
            self.assertNotEqual(result.returncode, 0)

    def test_foreign_configuration_drift_blocks_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            foreign = conf / "entityecho.conf"
            foreign.write_text("server { listen 80; }\n", encoding="utf-8")
            owned = conf / "wechat-memory-license.conf"
            digest = run_bash(
                'wkm_foreign_nginx_digest "$1" "$2"', str(root), str(owned)
            )
            self.assertEqual(digest.returncode, 0, digest.stderr)
            foreign.write_text("server { listen 8080; }\n", encoding="utf-8")
            result = run_bash(
                'wkm_assert_foreign_nginx_unchanged "$1" "$2" "$3"',
                digest.stdout.strip(),
                str(root),
                str(owned),
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
