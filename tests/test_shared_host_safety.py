from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "deploy" / "el9" / "shared-host-safety.sh"
TRANSACTION_LIBRARY = ROOT / "deploy" / "el9" / "deployment-transaction.sh"


def run_bash(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "$1"; source "$2"; shift 2; {script}',
            "bash",
            str(TRANSACTION_LIBRARY),
            str(LIBRARY),
            *arguments,
        ],
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
                "server { listen 80; server_name unrelated.example; }\n"
            )
            foreign.write_text(original, encoding="utf-8")
            owned = conf / "wechat-memory-license.conf"

            result = run_bash(
                'wkm_assert_no_foreign_nginx_conflict "$1" "$2" "$3" "$4"',
                str(root),
                str(owned),
                "104.244.90.172",
                "9000",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(foreign.read_text(encoding="utf-8"), original)

    def test_retirement_path_must_be_explicit_safe_and_conflicting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            owned = conf / "wechat-memory-license.conf"
            foreign = conf / "entityecho.conf"
            foreign.write_text(
                "server { listen 80; server_name entity-echo.com 104.244.90.172; }\n",
                encoding="utf-8",
            )
            command = (
                'wkm_validate_retire_nginx_conf "$1" "$2" "$3" "$4" "$5"'
            )
            result = run_bash(
                command,
                str(foreign),
                str(owned),
                str(conf),
                "104.244.90.172",
                "9000",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(foreign))

            unrelated = conf / "unrelated.conf"
            unrelated.write_text(
                "server { listen 80; server_name unrelated.example; }\n",
                encoding="utf-8",
            )
            result = run_bash(
                command,
                str(unrelated),
                str(owned),
                str(conf),
                "104.244.90.172",
                "9000",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(unrelated.is_file())

            outside = root / "outside.conf"
            outside.write_text(
                "server { listen 9000; server_name outside.example; }\n",
                encoding="utf-8",
            )
            result = run_bash(
                command,
                str(outside),
                str(owned),
                str(conf),
                "104.244.90.172",
                "9000",
            )
            self.assertNotEqual(result.returncode, 0)

            link = conf / "link.conf"
            link.symlink_to(foreign)
            result = run_bash(
                command,
                str(link),
                str(owned),
                str(conf),
                "104.244.90.172",
                "9000",
            )
            self.assertNotEqual(result.returncode, 0)

    def test_retirement_success_keeps_byte_exact_recovery_copy_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            source = conf / "entityecho.conf"
            original = (
                b"server { listen 80; server_name entity-echo.com "
                b"104.244.90.172; }\n# binary:\x00\xff\n"
            )
            source.write_bytes(original)
            source.chmod(0o640)
            transaction = root / "transaction"
            retired = root / "retired"
            result = run_bash(
                " ".join(
                    (
                        'wkm_retire_nginx_conf "$1" "$2" "$3" retired-conf',
                        '"104.244.90.172" "9000";',
                        'wkm_finalize_retired_nginx_conf "$2"',
                        '"104.244.90.172" "9000"',
                    )
                ),
                str(transaction),
                str(source),
                str(retired),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(source.exists())
            retirement_dirs = list(retired.glob("retirement.*"))
            self.assertEqual(len(retirement_dirs), 1)
            recovery = retirement_dirs[0] / "entityecho.conf"
            record = retirement_dirs[0] / "RETIREMENT.txt"
            self.assertEqual(recovery.read_bytes(), original)
            self.assertEqual(recovery.stat().st_mode & 0o777, 0o640)
            self.assertEqual(
                (transaction / "retired-conf.backup").read_bytes(), original
            )
            record_text = record.read_text(encoding="utf-8")
            self.assertIn("status=committed", record_text)
            self.assertIn(f"original_path={source}", record_text)
            self.assertIn(
                "replacement_endpoint=https://104.244.90.172:9000", record_text
            )

    def test_fault_after_retirement_restores_exact_bytes_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            source = conf / "entityecho.conf"
            original = b"server_name 104.244.90.172;\nlegacy:\x00\xfe\n"
            source.write_bytes(original)
            source.chmod(0o640)
            transaction = root / "transaction"
            retired = root / "retired"
            result = run_bash(
                " ".join(
                    (
                        'wkm_retire_nginx_conf "$1" "$2" "$3" retired-conf',
                        '"104.244.90.172" "9000";',
                        "set +e;",
                        "WKM_DEPLOY_ENABLE_FAULT_INJECTION=1",
                        "WKM_DEPLOY_TEST_FAULT=after_nginx_retirement",
                        "wkm_deploy_checkpoint after_nginx_retirement;",
                        "status=$?; set -e; [[ $status -eq 97 ]];",
                        'wkm_restore_retired_nginx_conf "$1" "$2" retired-conf',
                    )
                ),
                str(transaction),
                str(source),
                str(retired),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(source.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(retired.glob("retirement.*")), [])

    def test_fault_restores_legacy_owned_config_and_retired_site_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            owned = conf / "wechat-memory-license.conf"
            retired_site = conf / "entityecho.conf"
            old_owned = (
                b"# historical wrong endpoint\nserver_name 104.224.90.172;\n"
                b"location / { root /var/www/wechat-memory-license-acme; }\n"
            )
            old_site = (
                b"server { listen 80; server_name entity-echo.com "
                b"104.244.90.172; }\n"
            )
            owned.write_bytes(old_owned)
            retired_site.write_bytes(old_site)
            transaction = root / "transaction"
            retired = root / "retired"
            result = run_bash(
                " ".join(
                    (
                        'wkm_tx_capture_file "$1" "$2" nginx-config;',
                        'wkm_retire_nginx_conf "$1" "$3" "$4" retired-conf',
                        '"104.244.90.172" "9000";',
                        'printf "server_name 104.244.90.172;\\n" >"$2";',
                        "set +e;",
                        "WKM_DEPLOY_ENABLE_FAULT_INJECTION=1",
                        "WKM_DEPLOY_TEST_FAULT=after_nginx_retirement",
                        "wkm_deploy_checkpoint after_nginx_retirement;",
                        "status=$?; set -e; [[ $status -eq 97 ]];",
                        'wkm_tx_restore_file "$1" "$2" nginx-config;',
                        'wkm_restore_retired_nginx_conf "$1" "$3" retired-conf',
                    )
                ),
                str(transaction),
                str(owned),
                str(retired_site),
                str(retired),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(owned.read_bytes(), old_owned)
            self.assertEqual(retired_site.read_bytes(), old_site)

    def test_retirement_rollback_never_overwrites_concurrent_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            source = conf / "entityecho.conf"
            source.write_text(
                "server_name 104.244.90.172;\n", encoding="utf-8"
            )
            transaction = root / "transaction"
            retired = root / "retired"
            result = run_bash(
                " ".join(
                    (
                        'wkm_retire_nginx_conf "$1" "$2" "$3" retired-conf',
                        '"104.244.90.172" "9000";',
                        'printf "concurrent admin change\\n" >"$2";',
                        'wkm_restore_retired_nginx_conf "$1" "$2" retired-conf',
                    )
                ),
                str(transaction),
                str(source),
                str(retired),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                source.read_text(encoding="utf-8"), "concurrent admin change\n"
            )
            recovery = next(retired.glob("retirement.*/entityecho.conf"))
            self.assertEqual(
                recovery.read_text(encoding="utf-8"),
                "server_name 104.244.90.172;\n",
            )

    def test_tampered_retirement_copy_is_preserved_during_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            source = conf / "entityecho.conf"
            original = b"server_name 104.244.90.172;\n"
            source.write_bytes(original)
            transaction = root / "transaction"
            retired = root / "retired"
            result = run_bash(
                " ".join(
                    (
                        'wkm_retire_nginx_conf "$1" "$2" "$3" retired-conf',
                        '"104.244.90.172" "9000";',
                        'printf "unexpected replacement\\n"',
                        '>"${WKM_RETIRED_NGINX_FILE}";',
                        'wkm_restore_retired_nginx_conf "$1" "$2" retired-conf',
                    )
                ),
                str(transaction),
                str(source),
                str(retired),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_bytes(), original)
            recovery = next(retired.glob("retirement.*/entityecho.conf"))
            self.assertEqual(recovery.read_text(), "unexpected replacement\n")

    def test_legacy_wrong_ip_wechat_config_is_recognized_for_safe_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            owned = Path(temporary) / "wechat-memory-license.conf"
            owned.write_text(
                "# historical wrong endpoint: 104.224.90.172\n"
                "access_log /var/log/nginx/wechat-memory-license-acme.access.log;\n"
                "location / { root /var/www/wechat-memory-license-acme; }\n",
                encoding="utf-8",
            )
            result = run_bash('wkm_assert_owned_nginx_file "$1"', str(owned))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_exact_ip_or_https_port_collision_fails_closed(self) -> None:
        for body in (
            "server { listen 80; server_name 104.244.90.172; }\n",
            "server { listen 80; server_name\n  104.244.90.172; }\n",
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
                    "104.244.90.172",
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

            dangling = root / "dangling.conf"
            dangling.symlink_to(root / "missing.conf")
            result = run_bash('wkm_assert_owned_nginx_file "$1"', str(dangling))
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
