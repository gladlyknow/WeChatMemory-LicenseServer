from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentTransactionTests(unittest.TestCase):
    def test_transaction_primitives_restore_exact_pre_deploy_state(self) -> None:
        result = subprocess.run(
            ["bash", "deploy/el9/test-deployment-transaction.sh"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deployment transaction self-test: ok", result.stdout)

    def test_release_dependencies_are_isolated_and_runtime_is_pinned(self) -> None:
        installer = (ROOT / "deploy/el9/install-release.sh").read_text(
            encoding="utf-8"
        )
        service = (
            ROOT / "deploy/el9/wechat-memory-license.service.example"
        ).read_text(encoding="utf-8")
        admin = (ROOT / "deploy/el9/wkm-license-admin").read_text(encoding="utf-8")

        self.assertIn('RELEASE_VENV="${RELEASE_DIR}/.venv"', installer)
        self.assertIn('python3.11 -m venv "${RELEASE_VENV}"', installer)
        self.assertIn('"${RELEASE_VENV}/bin/python" -m pip install', installer)
        self.assertIn('"${RELEASE_VENV}/bin/python" -m pip check', installer)
        self.assertIn("PRIVATE_KEY_HITS", installer)
        self.assertIn('[[ -e "${RELEASE_DIR}/.secrets" ]]', installer)
        self.assertIn("BEGIN (RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY", installer)
        self.assertIn('"${RELEASE_DIR}/scripts/generate_sbom.py"', installer)
        self.assertIn('--check "${RELEASE_DIR}/sbom/server-runtime.spdx.json"', installer)
        self.assertIn('cd "${RELEASE_DIR}"', installer)
        self.assertIn('"${RELEASE_TEST_VENV}/bin/python" -m pytest', installer)
        self.assertIn(
            "/usr/sbin/runuser -u wkm-license -- /usr/bin/env", installer
        )
        self.assertEqual(
            installer.count("wkm_prepare_root_owned_runtime_venv"), 2
        )
        runtime_permissions = installer.index(
            'wkm_prepare_root_owned_runtime_venv "${RELEASE_VENV}" wkm-license'
        )
        test_copy = installer.index(
            'cp -a -- "${RELEASE_VENV}" "${RELEASE_TEST_VENV}"'
        )
        test_permissions = installer.index(
            'wkm_prepare_root_owned_runtime_venv "${RELEASE_TEST_VENV}" wkm-license'
        )
        unprivileged_tests = installer.index(
            "/usr/sbin/runuser -u wkm-license -- /usr/bin/env"
        )
        signing_verification = installer.index(
            '"${RELEASE_VENV}/bin/python" -m license_server signing-key verify'
        )
        runtime_probe = installer.index(
            "from scripts.service_preflight import validate_wsgi_runtime;"
        )
        migration = installer.index("DATABASE_MIGRATION_ATTEMPTED=1")
        self.assertLess(test_copy, test_permissions)
        self.assertLess(test_permissions, unprivileged_tests)
        self.assertLess(unprivileged_tests, signing_verification)
        self.assertLess(signing_verification, runtime_permissions)
        self.assertLess(runtime_permissions, runtime_probe)
        self.assertLess(runtime_probe, migration)
        self.assertIn(
            '"${RELEASE_VENV}/bin/python" -c', installer[runtime_permissions:runtime_probe]
        )
        self.assertIn("PYTHONWARNINGS=error::SyntaxWarning", installer)
        self.assertIn("bash -n deploy/el9/*.sh deploy/el9/wkm-license-admin", installer)
        self.assertIn("bash deploy/el9/test-deployment-transaction.sh", installer)
        self.assertNotIn('"${APP_ROOT}/venv/bin/pip"', installer)
        self.assertNotIn('"${APP_ROOT}/venv/bin/python"', installer)
        self.assertIn("WorkingDirectory=@WKM_RELEASE_DIR@", service)
        self.assertIn(
            "ExecStart=@WKM_RELEASE_DIR@/.venv/bin/python", service
        )
        self.assertIn(
            "ExecStartPre=@WKM_RELEASE_DIR@/.venv/bin/python", service
        )
        self.assertIn("@WKM_RELEASE_DIR@/scripts/service_preflight.py", service)
        self.assertIn(
            "--manifest @WKM_RELEASE_DIR@/config/signing-public-keys.json",
            service,
        )
        self.assertNotIn("/opt/wechat-memory-license/current", service)
        self.assertIn("Environment=PYTHONDONTWRITEBYTECODE=1", service)
        self.assertIn(
            "SIGNING_PUBLIC_KEYS_PATH=${MANIFEST_PATH}", installer
        )
        self.assertIn(
            'sed "s|@WKM_RELEASE_DIR@|${RELEASE_DIR}|g"', installer
        )
        self.assertIn(
            "systemd 单元模板未完整渲染或仍引用 current", installer
        )
        self.assertEqual(admin.count('readlink -e -- "${CURRENT_LINK}"'), 1)
        self.assertIn('PYTHON="${RELEASE_DIR}/.venv/bin/python"', admin)
        self.assertIn('ADMIN="${RELEASE_DIR}/license_admin.py"', admin)
        self.assertIn("^[0-9]{8}T[0-9]{6}Z-[0-9]+$", admin)
        self.assertNotIn("current/.venv", admin)
        self.assertIn(
            "from scripts.service_preflight import validate_wsgi_runtime", installer
        )

    def test_rendered_service_keeps_one_release_across_current_switch(self) -> None:
        template = (
            ROOT / "deploy/el9/wechat-memory-license.service.example"
        ).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            release_a = releases / "20261030T120000Z-100"
            release_b = releases / "20261030T120001Z-101"
            for release, marker in ((release_a, "a"), (release_b, "b")):
                site = release / "site"
                site.mkdir(parents=True)
                (site / "release_marker.py").write_text(
                    f'VALUE = "{marker}"\n', encoding="utf-8"
                )

            current = root / "current"
            current.symlink_to(release_a, target_is_directory=True)
            rendered = template.replace("@WKM_RELEASE_DIR@", str(release_a))
            self.assertNotIn("@WKM_RELEASE_DIR@", rendered)
            self.assertNotIn(str(current), rendered)
            working_directory = next(
                line.split("=", 1)[1]
                for line in rendered.splitlines()
                if line.startswith("WorkingDirectory=")
            )
            self.assertEqual(Path(working_directory), release_a)

            program = (
                "import sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "print('ready', flush=True)\n"
                "sys.stdin.readline()\n"
                "import release_marker\n"
                "print(release_marker.VALUE, flush=True)\n"
            )

            def import_after_switch(search_path: Path) -> str:
                current_next = root / "current.next"
                if current_next.exists() or current_next.is_symlink():
                    current_next.unlink()
                current_next.symlink_to(release_a, target_is_directory=True)
                os.replace(current_next, current)
                process = subprocess.Popen(
                    [os.sys.executable, "-c", program, str(search_path)],
                    cwd=working_directory,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert process.stdout is not None
                self.assertEqual(process.stdout.readline().strip(), "ready")
                current_next.symlink_to(release_b, target_is_directory=True)
                os.replace(current_next, current)
                stdout, stderr = process.communicate("\n", timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                return stdout.strip()

            # The dynamic path demonstrates the original failure mechanism;
            # the rendered WorkingDirectory remains pinned to release A.
            self.assertEqual(import_after_switch(current / "site"), "b")
            self.assertEqual(
                import_after_switch(Path(working_directory) / "site"), "a"
            )

    def test_release_permission_policy_is_deterministic_and_fail_closed(self) -> None:
        policy = ROOT / "deploy/el9/release-permissions.sh"
        expected_executables = {
            "deploy/el9/backup-database.sh",
            "deploy/el9/deploy-from-data.sh",
            "deploy/el9/deployment-transaction.sh",
            "deploy/el9/install-release.sh",
            "deploy/el9/one-click-deploy.sh",
            "deploy/el9/test-deployment-transaction.sh",
            "deploy/el9/wkm-license-admin",
            "register_user.py",
            "scripts/check_documentation.py",
            "tests/fixtures/ip-command-shim.sh",
            "tests/fixtures/selinux-command-shim.sh",
        }

        with tempfile.TemporaryDirectory() as temporary:
            release = Path(temporary) / "release"
            release.mkdir()
            ordinary = release / "ordinary.txt"
            ordinary.write_text("ordinary\n", encoding="utf-8")
            ordinary.chmod(0o777)
            for relative in expected_executables:
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                path.chmod(0o600)

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; wkm_normalize_release_permissions "$2"',
                    "bash",
                    str(policy),
                    str(release),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(ordinary.stat().st_mode), 0o644)
            self.assertTrue(
                all(
                    stat.S_IMODE((release / path).stat().st_mode) == 0o755
                    for path in expected_executables
                )
            )

            # Replacing any allow-listed entry with a symlink must fail rather
            # than chmod an external target.
            victim = release / "register_user.py"
            victim.unlink()
            victim.symlink_to(ordinary)
            ordinary.chmod(0o644)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    'source "$1"; wkm_normalize_release_permissions "$2"',
                    "bash",
                    str(policy),
                    str(release),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(stat.S_IMODE(ordinary.stat().st_mode), 0o644)

        if (ROOT / ".git").is_dir():
            tracked = subprocess.run(
                ["git", "ls-files", "--stage"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            tracked_executables = {
                line.split("\t", 1)[1]
                for line in tracked
                if line.startswith("100755 ")
            }
            self.assertEqual(tracked_executables, expected_executables)

        # The helper's public list is the same contract for Git archives and
        # uploaded bundles, independent of their incoming mode bits.
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; wkm_release_executable_paths',
                "bash",
                str(policy),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(set(result.stdout.splitlines()), expected_executables)

    def test_runtime_venv_permission_contract_rejects_writable_group(self) -> None:
        policy = ROOT / "deploy/el9/release-permissions.sh"
        with tempfile.TemporaryDirectory() as temporary:
            venv = Path(temporary) / "venv"
            executable = venv / "bin/python"
            package = venv / "lib/site-packages/example.py"
            executable.parent.mkdir(parents=True)
            package.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            package.write_text("VALUE = 1\n", encoding="utf-8")
            for directory in (venv, executable.parent, package.parent.parent, package.parent):
                directory.chmod(0o750)
            executable.chmod(0o750)
            package.chmod(0o640)

            arguments = [
                "bash",
                "-c",
                'source "$1"; wkm_assert_runtime_venv_permissions "$2" "$3" "$4"',
                "bash",
                str(policy),
                str(venv),
                str(os.getuid()),
                str(os.getgid()),
            ]
            result = subprocess.run(
                arguments, check=False, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            package.chmod(0o660)
            result = subprocess.run(
                arguments, check=False, capture_output=True, text=True
            )
            self.assertNotEqual(result.returncode, 0)

        policy_text = policy.read_text(encoding="utf-8")
        self.assertIn('-exec chown 0:"${runtime_group_id}" -- {} +', policy_text)
        self.assertIn('chmod -R u=rwX,g=rX,o= "${venv_dir}"', policy_text)
        self.assertNotIn("chown -R wkm-license", policy_text)

    def test_runtime_configuration_contains_only_consumed_settings(self) -> None:
        installer = (ROOT / "deploy/el9/install-release.sh").read_text(
            encoding="utf-8"
        )
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        service = (
            ROOT / "deploy/el9/wechat-memory-license.service.example"
        ).read_text(encoding="utf-8")

        for unused in ("PUBLIC_BASE_URL=", "APP_BIND=", "APP_PORT=", "LOG_LEVEL="):
            self.assertNotIn(unused, installer)
            self.assertNotIn(unused, example)
        self.assertIn("--host 127.0.0.1 --port 9100", service)
        self.assertIn("Type=exec", service)
        self.assertIn(
            "AssertPathExists=@WKM_RELEASE_DIR@/license_server/__main__.py",
            service,
        )
        self.assertNotIn("ConditionPathExists=", service)
        self.assertIn(
            "scripts/service_preflight.py --env /etc/wechat-memory-license/service.env",
            service,
        )
        self.assertNotIn("/opt/wechat-memory-license/current", service)
        self.assertIn("REFRESH_TOKEN_DAYS=365", example)
        self.assertIn("REFRESH_TOKEN_DAYS=$(env_value REFRESH_TOKEN_DAYS 365)", installer)

    def test_release_failure_contract_arms_rollback_before_runtime_mutation(self) -> None:
        installer = (ROOT / "deploy/el9/install-release.sh").read_text(
            encoding="utf-8"
        )
        capture = installer.index(
            'wkm_tx_capture_link "${INSTALL_TRANSACTION_DIR}" "${CURRENT_LINK}" current'
        )
        arm = installer.index("INSTALL_RUNTIME_ARMED=1")
        environment_install = installer.index(
            'install -o root -g wkm-license -m 0640 "${ENV_TMP}" "${ENV_PATH}"'
        )
        candidate_pip = installer.index(
            '"${RELEASE_VENV}/bin/python" -m pip install --no-cache-dir'
        )
        private_key_gate = installer.index("PRIVATE_KEY_HITS")
        sbom_gate = installer.index('"${RELEASE_DIR}/scripts/generate_sbom.py"')
        full_tests = installer.index('"${RELEASE_TEST_VENV}/bin/python" -m pytest')
        shell_gate = installer.index("bash -n deploy/el9/*.sh")
        transaction_gate = installer.index(
            "bash deploy/el9/test-deployment-transaction.sh"
        )
        migration = installer.index("DATABASE_MIGRATION_ATTEMPTED=1")
        service_preflight = installer.index(
            '"${RELEASE_DIR}/scripts/service_preflight.py"'
        )
        unit_render = installer.index(
            'sed "s|@WKM_RELEASE_DIR@|${RELEASE_DIR}|g"'
        )
        switch = installer.index(
            'mv -Tf "${CURRENT_LINK}.next" "${CURRENT_LINK}"'
        )
        local_acceptance = installer.index(
            "http://127.0.0.1:9100/readyz/deep"
        )
        commit = installer.index("INSTALL_DEPLOY_COMMITTED=1")

        self.assertLess(capture, arm)
        self.assertLess(arm, environment_install)
        self.assertLess(private_key_gate, candidate_pip)
        self.assertLess(candidate_pip, sbom_gate)
        self.assertLess(sbom_gate, full_tests)
        self.assertLess(full_tests, shell_gate)
        self.assertLess(shell_gate, transaction_gate)
        self.assertLess(transaction_gate, environment_install)
        self.assertLess(sbom_gate, environment_install)
        self.assertLess(unit_render, migration)
        self.assertLess(environment_install, migration)
        self.assertLess(migration, service_preflight)
        self.assertLess(service_preflight, switch)
        self.assertLess(migration, switch)
        self.assertLess(switch, local_acceptance)
        self.assertLess(local_acceptance, commit)
        normal_stop = installer.index(
            "wkm_stop_systemd_units_and_verify_inactive",
            unit_render,
        )
        normal_stop_block = installer[normal_stop:environment_install]
        self.assertIn("wechat-memory-license-backup.timer", normal_stop_block)
        self.assertIn("wechat-memory-license-backup.service", normal_stop_block)
        self.assertIn("wechat-memory-license.service", normal_stop_block)
        self.assertLess(normal_stop, environment_install)
        self.assertIn("rollback_install_runtime", installer)
        self.assertIn("wkm_tx_restore_link", installer)
        self.assertIn("wkm_tx_restore_file", installer)
        self.assertIn("不会在服务可能产生新写入后自动降级数据库", installer)
        self.assertIn("emit_service_failure_diagnostics", installer)
        self.assertIn("journalctl -u wechat-memory-license", installer)
        self.assertIn("redact_service_diagnostics", installer)
        self.assertIn("[REDACTED-PRIVATE-KEY-BLOCK]", installer)
        self.assertIn("[REDACTED-SENSITIVE-LINE]", installer)
        self.assertIn("systemctl show wechat-memory-license", installer)
        self.assertIn("-p ActiveState -p SubState -p Result", installer)
        self.assertIn("-p ConditionResult -p AssertResult", installer)
        self.assertIn("diagnose_path_metadata signing_key", installer)
        self.assertIn("--since \"${SERVICE_START_DIAGNOSTIC_SINCE:--5 minutes}\"", installer)
        self.assertIn("for _attempt in $(seq 1 30)", installer)
        self.assertIn("sleep 0.5", installer)
        self.assertLess(
            installer.index("systemctl restart wechat-memory-license"),
            installer.index("for _attempt in $(seq 1 30)"),
        )
        rollback = installer.index("rollback_install_runtime()")
        rollback_stop = installer.index(
            "wkm_stop_systemd_units_and_verify_inactive", rollback
        )
        rollback_current_restore = installer.index(
            'wkm_tx_restore_link "${INSTALL_TRANSACTION_DIR}" "${CURRENT_LINK}" current',
            rollback,
        )
        rollback_stop_block = installer[rollback_stop:rollback_current_restore]
        self.assertIn("wechat-memory-license-backup.timer", rollback_stop_block)
        self.assertIn("wechat-memory-license-backup.service", rollback_stop_block)
        self.assertIn("wechat-memory-license.service", rollback_stop_block)
        self.assertLess(rollback_stop, rollback_current_restore)

    def test_one_click_failure_contract_spans_application_and_nginx(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        trap_install = script.index("trap on_deploy_exit EXIT")
        capture_call = script.index("\ncapture_application_runtime\n", trap_install)
        application_install = script.index(
            'bash "${SOURCE_DIR}/deploy/el9/install-release.sh"'
        )
        certbot_hook_install = script.index(
            '"${SOURCE_DIR}/deploy/el9/certbot-deploy-hook.sh"'
        )
        certbot_compensation_armed = script.rfind(
            "APPLICATION_RUNTIME_CHANGED=1", 0, certbot_hook_install
        )
        application_checkpoint = script.index(
            "wkm_deploy_checkpoint after_application_install"
        )
        nginx_switch = script.index(
            'install -o root -g root -m 0644 "${PRODUCTION_TMP}" "${BOOTSTRAP}"'
        )
        nginx_checkpoint = script.index("wkm_deploy_checkpoint after_nginx_switch")
        public_acceptance = script.index(
            '"${PUBLIC_SCHEME}://${PUBLIC_IP}:${PUBLIC_PORT}/readyz"'
        )
        commit = script.index("DEPLOY_COMPLETED=1")

        self.assertLess(trap_install, capture_call)
        self.assertLess(capture_call, application_install)
        self.assertGreater(certbot_compensation_armed, capture_call)
        self.assertLess(certbot_compensation_armed, certbot_hook_install)
        self.assertLess(application_install, application_checkpoint)
        self.assertLess(application_checkpoint, nginx_switch)
        self.assertLess(nginx_switch, nginx_checkpoint)
        self.assertLess(nginx_checkpoint, public_acceptance)
        self.assertLess(public_acceptance, commit)
        for token in (
            "restore_application_runtime",
            "restore_nginx_runtime",
            "/opt/wechat-memory-license/current",
            "/etc/wechat-memory-license/service.env",
            "/etc/systemd/system/wechat-memory-license.service",
            "APP_SERVICE_WAS_ACTIVE",
            "NGINX_WAS_ACTIVE",
        ):
            self.assertIn(token, script)
        self.assertIn("SQLite init/migration", script)
        self.assertIn(".one-click-transaction.*", script)
        self.assertIn(".install-transaction.*", script)
        rollback = script.index("restore_application_runtime()")
        rollback_stop = script.index(
            "wkm_stop_systemd_units_and_verify_inactive", rollback
        )
        rollback_current_restore = script.index(
            'wkm_tx_restore_link "${APPLICATION_TRANSACTION_DIR}"', rollback
        )
        rollback_daemon_reload = script.index("systemctl daemon-reload", rollback)
        rollback_state_restore = script.index(
            "restore_one_click_unit_state wechat-memory-license", rollback
        )
        rollback_stop_block = script[rollback_stop:rollback_current_restore]
        for unit in (
            "wechat-memory-license-backup.timer",
            "wechat-memory-license-backup.service",
            "wechat-memory-license.service",
            "wechat-memory-certbot-renew.timer",
            "wechat-memory-certbot-renew.service",
        ):
            self.assertIn(unit, rollback_stop_block)
        self.assertLess(rollback_stop, rollback_current_restore)
        self.assertLess(rollback_current_restore, rollback_daemon_reload)
        self.assertLess(rollback_daemon_reload, rollback_state_restore)

    def test_one_click_serializes_host_mutations_with_kernel_lock(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        lock = script.index("DEPLOY_LOCK_PATH=/run/wechat-memory-license-deploy.lock")
        stale_transaction_check = script.index("STALE_TRANSACTIONS=(")
        package_install = script.index("dnf install -y nginx")
        application_capture = script.index("capture_application_runtime")
        self.assertIn('exec {DEPLOY_LOCK_FD}<>"${DEPLOY_LOCK_PATH}"', script)
        self.assertIn('flock -n "${DEPLOY_LOCK_FD}"', script)
        self.assertIn('WKM_DEPLOY_LOCK_INHERITED:-0', script)
        self.assertIn('export WKM_DEPLOY_LOCK_FD="${DEPLOY_LOCK_FD}"', script)
        self.assertIn("[[ -L \"${DEPLOY_LOCK_PATH}\" ]]", script)
        self.assertLess(lock, stale_transaction_check)
        self.assertLess(lock, package_install)
        self.assertLess(lock, application_capture)

    def test_all_mutating_entrypoints_share_the_deployment_lock(self) -> None:
        wrapper = (ROOT / "deploy/el9/deploy-from-data.sh").read_text(
            encoding="utf-8"
        )
        one_click = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "deploy/el9/install-release.sh").read_text(
            encoding="utf-8"
        )
        admin = (ROOT / "deploy/el9/wkm-license-admin").read_text(
            encoding="utf-8"
        )
        backup = (ROOT / "deploy/el9/backup-database.sh").read_text(
            encoding="utf-8"
        )

        wrapper_lock = wrapper.index(
            "DEPLOY_LOCK_PATH=/run/wechat-memory-license-deploy.lock"
        )
        wrapper_status = wrapper.index('git -C "${SOURCE_DIR}" status --porcelain')
        wrapper_fetch = wrapper.index('git -C "${SOURCE_DIR}" fetch')
        self.assertLess(wrapper_lock, wrapper_status)
        self.assertLess(wrapper_lock, wrapper_fetch)
        self.assertIn("export WKM_DEPLOY_LOCK_INHERITED=1", wrapper)
        self.assertIn('export WKM_DEPLOY_LOCK_FD="${DEPLOY_LOCK_FD}"', wrapper)
        self.assertIn("export WKM_DEPLOY_LOCK_TOKEN", wrapper)

        self.assertIn('WKM_DEPLOY_LOCK_INHERITED:-0', one_click)
        self.assertIn('flock -n "${DEPLOY_LOCK_FD}"', one_click)
        self.assertIn("WKM_DEPLOY_LOCK_TOKEN", one_click)
        self.assertIn('WKM_DEPLOY_LOCK_INHERITED:-0', installer)
        self.assertIn('flock -n "${WKM_DEPLOY_LOCK_FD}"', installer)
        self.assertIn("WKM_DEPLOY_LOCK_TOKEN", installer)
        self.assertIn('exec {INSTALL_DEPLOY_LOCK_FD}<>"${DEPLOY_LOCK_PATH}"', installer)
        self.assertIn('flock -n "${INSTALL_DEPLOY_LOCK_FD}"', installer)
        self.assertLess(
            installer.index("DEPLOY_LOCK_PATH=/run/wechat-memory-license-deploy.lock"),
            installer.index("APP_ROOT=/opt/wechat-memory-license"),
        )

        self.assertIn('flock -s -n "${ADMIN_LOCK_FD}"', admin)
        self.assertIn('flock -s -n "${BACKUP_LOCK_FD}"', backup)
        self.assertLess(admin.index("flock -s -n"), admin.index("readlink -e"))
        self.assertLess(
            backup.index("flock -s -n"), backup.index("/usr/bin/sqlite3")
        )

        # Re-locking the inherited descriptor is safe because it refers to the
        # same open-file description.  A separately opened descriptor remains
        # blocked, which is the serialization property the wrappers rely on.
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "deploy.lock"
            probe = subprocess.run(
                [
                    "bash",
                    "-c",
                    """
set -euo pipefail
exec {lock_fd}<>\"$1\"
flock -n \"${lock_fd}\"
WKM_DEPLOY_LOCK_FD=\"${lock_fd}\" bash -c 'flock -n \"${WKM_DEPLOY_LOCK_FD}\"'
if bash -c 'exec {other_fd}<>\"$1\"; flock -n \"${other_fd}\"' bash \"$1\"; then
  exit 9
fi
""",
                    "bash",
                    str(lock_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_systemd_stop_gate_is_ordered_and_fail_closed(self) -> None:
        helper = ROOT / "deploy/el9/deployment-transaction.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"
            state_dir = root / "state"
            bin_dir.mkdir()
            state_dir.mkdir()
            systemctl = bin_dir / "systemctl"
            systemctl.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
command_name=\"$1\"
unit=\"${2:-}\"
case \"${command_name}\" in
  show)
    property=\"\"
    for argument in \"$@\"; do
      case \"${argument}\" in
        --property=*) property=\"${argument#--property=}\" ;;
      esac
    done
    case \"${property}\" in
      LoadState) cat \"${WKM_SYSTEMD_STATE}/${unit}.load\" ;;
      ActiveState) cat \"${WKM_SYSTEMD_STATE}/${unit}.active\" ;;
      *) exit 2 ;;
    esac
    ;;
  stop)
    printf '%s\\n' \"${unit}\" >>\"${WKM_SYSTEMD_STATE}/calls\"
    if [[ \"${unit}\" == \"${WKM_FAIL_UNIT:-}\" ]]; then
      exit 1
    fi
    printf 'inactive\\n' >\"${WKM_SYSTEMD_STATE}/${unit}.active\"
    ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            systemctl.chmod(0o700)

            units = ("backup.timer", "backup.service", "api.service")
            for unit, load, active in (
                (units[0], "loaded", "active"),
                # A missing unit file does not prove that an already-loaded
                # process has stopped; the helper must still stop this state.
                (units[1], "not-found", "active"),
                (units[2], "loaded", "inactive"),
            ):
                (state_dir / f"{unit}.load").write_text(load + "\n", encoding="utf-8")
                (state_dir / f"{unit}.active").write_text(
                    active + "\n", encoding="utf-8"
                )

            environment = dict(os.environ)
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
            environment["WKM_SYSTEMD_STATE"] = str(state_dir)
            marker = root / "restored"
            command = (
                'source "$1"; '
                'if ! wkm_stop_systemd_units_and_verify_inactive '
                'backup.timer backup.service api.service; then exit 1; fi; '
                'printf restored >"$2"'
            )
            result = subprocess.run(
                ["bash", "-c", command, "bash", str(helper), str(marker)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (state_dir / "calls").read_text(encoding="utf-8").splitlines(),
                list(units),
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "restored")

            # A failed stop prevents the simulated restore step.  It must not
            # be downgraded to a warning while current/env/unit are changed.
            marker.unlink()
            (state_dir / "calls").unlink()
            for unit in units:
                (state_dir / f"{unit}.active").write_text(
                    "active\n", encoding="utf-8"
                )
            environment["WKM_FAIL_UNIT"] = "backup.service"
            result = subprocess.run(
                ["bash", "-c", command, "bash", str(helper), str(marker)],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertEqual(
                (state_dir / "calls").read_text(encoding="utf-8").splitlines(),
                ["backup.timer", "backup.service"],
            )

    def test_default_public_entry_is_http_without_certbot_dependency(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        wrapper = (ROOT / "deploy/el9/deploy-from-data.sh").read_text(
            encoding="utf-8"
        )
        http_nginx = (ROOT / "deploy/el9/nginx-production.conf").read_text(
            encoding="utf-8"
        )
        tls_nginx = (
            ROOT / "deploy/el9/nginx-production-tls.conf"
        ).read_text(encoding="utf-8")
        existing_tls_nginx = (
            ROOT / "deploy/el9/nginx-production-existing-tls.conf"
        ).read_text(encoding="utf-8")

        self.assertIn('TLS_MODE="http"', script)
        self.assertIn('TLS_MODE="http"', wrapper)
        self.assertIn('ACK_CLIENT_HTTPS_ORIGIN_READY=0', script)
        self.assertIn('ACK_CLIENT_HTTPS_ORIGIN_READY=0', wrapper)
        self.assertIn('--ack-client-https-origin-ready)', script)
        self.assertIn('--ack-client-https-origin-ready)', wrapper)
        self.assertIn('--tls-mode "${TLS_MODE}"', wrapper)
        self.assertIn('PUBLIC_SCHEME="http"', script)
        self.assertIn('"${PUBLIC_SCHEME}://${PUBLIC_IP}:${PUBLIC_PORT}/readyz"', script)
        self.assertIn('"${PUBLIC_SCHEME}://${PUBLIC_IP}:${PUBLIC_PORT}/v1/keys"', script)
        self.assertIn('[[ "${TLS_MODE}" == "letsencrypt" ]]', script)
        self.assertIn('elif [[ "${TLS_MODE}" == "existing" ]]', script)
        compatibility_gate = (
            '[[ "${TLS_MODE}" != "http" && '
            '"${ACK_CLIENT_HTTPS_ORIGIN_READY}" != "1" ]]'
        )
        self.assertIn(compatibility_gate, script)
        self.assertIn(compatibility_gate, wrapper)
        self.assertLess(script.index(compatibility_gate), script.index("dnf install -y"))
        self.assertLess(wrapper.index(compatibility_gate), wrapper.index("git -C"))
        self.assertIn('arguments+=(--ack-client-https-origin-ready)', wrapper)

        self.assertIn("listen 9000;", http_nginx)
        self.assertNotIn("listen 9000 ssl", http_nginx)
        self.assertNotIn("ssl_certificate", http_nginx)
        self.assertIn("proxy_pass http://127.0.0.1:9100;", http_nginx)
        self.assertIn("X-Forwarded-Proto http", http_nginx)
        self.assertIn("listen 9000 ssl;", tls_nginx)
        self.assertIn("ssl_certificate", tls_nginx)
        self.assertIn("listen 80;", tls_nginx)
        self.assertIn("listen 9000 ssl;", existing_tls_nginx)
        self.assertNotIn("listen 80;", existing_tls_nginx)
        self.assertNotIn("acme-challenge", existing_tls_nginx)
        self.assertIn("nginx-production-existing-tls.conf", script)

        # The default branch never creates the ACME webroot or executes the
        # Certbot environment.  Those operations are all inside the explicit
        # letsencrypt branch.
        letsencrypt_branch = script.index('if [[ "${TLS_MODE}" == "letsencrypt" ]]; then')
        certbot_call = script.index('"${CERTBOT_ROOT}/bin/certbot" certonly')
        self.assertLess(letsencrypt_branch, certbot_call)
        self.assertIn("retire_certbot_runtime_if_unused", script)
        self.assertNotIn("/opt/certbot", script)

    def test_tls_origin_ack_gate_is_fail_closed_before_host_mutation(self) -> None:
        wrapper = (ROOT / "deploy/el9/deploy-from-data.sh").read_text(
            encoding="utf-8"
        )
        prefix, separator, _remainder = wrapper.partition(
            'if [[ ${EUID} -ne 0 ]]; then'
        )
        self.assertTrue(separator)
        option_probe = (
            prefix
            + '\nprintf "accepted:%s:%s\\n" "${TLS_MODE}" '
            + '"${ACK_CLIENT_HTTPS_ORIGIN_READY}"\n'
        )

        def probe(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", "-s", "--", *arguments],
                input=option_probe,
                text=True,
                capture_output=True,
                check=False,
            )

        http = probe("--tls-mode", "http")
        self.assertEqual(http.returncode, 0, http.stderr)
        self.assertIn("accepted:http:0", http.stdout)

        tls_without_ack = probe("--tls-mode", "existing")
        self.assertEqual(tls_without_ack.returncode, 2)
        self.assertNotIn("accepted:", tls_without_ack.stdout)
        self.assertIn("--ack-client-https-origin-ready", tls_without_ack.stderr)

        tls_with_ack = probe(
            "--tls-mode", "existing", "--ack-client-https-origin-ready"
        )
        self.assertEqual(tls_with_ack.returncode, 0, tls_with_ack.stderr)
        self.assertIn("accepted:existing:1", tls_with_ack.stdout)

    def test_existing_tls_is_snapshotted_before_mutation_and_restored(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        capture_call = script.index("\ncapture_application_runtime\n")
        existing_branch = script.index(
            'if [[ ! -f "${EXISTING_CERT}" || ! -f "${EXISTING_KEY}" ]]'
        )
        compensation_armed = script.index(
            "APPLICATION_RUNTIME_CHANGED=1", existing_branch
        )
        certificate_install = script.index(
            'install -o root -g nginx -m 0640 "${EXISTING_CERT}"',
            existing_branch,
        )

        for path, label in (
            (
                "/etc/nginx/wechat-memory-license-tls/fullchain.pem",
                "existing-tls-cert",
            ),
            (
                "/etc/nginx/wechat-memory-license-tls/privkey.pem",
                "existing-tls-key",
            ),
        ):
            self.assertGreaterEqual(script.count(path), 3)
            self.assertGreaterEqual(script.count(label), 2)
        self.assertLess(capture_call, compensation_armed)
        self.assertLess(compensation_armed, certificate_install)

    def test_optional_certbot_version_has_format_and_numeric_minimum_gates(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        helper = (ROOT / "deploy/el9/deployment-transaction.sh").read_text(
            encoding="utf-8"
        )
        self_test = (
            ROOT / "deploy/el9/test-deployment-transaction.sh"
        ).read_text(encoding="utf-8")

        format_gate = script.index(
            '"${CERTBOT_VERSION}" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$'
        )
        minimum_gate = script.index(
            'wkm_version_at_least "${CERTBOT_VERSION}" 5 4'
        )
        package_install = script.index('"certbot==${CERTBOT_VERSION}"')
        self.assertLess(format_gate, minimum_gate)
        self.assertLess(minimum_gate, package_install)
        self.assertIn("wkm_normalize_decimal", helper)
        self.assertIn("wkm_decimal_at_least", helper)
        for version in ("5.4.0", "5.9.0", "5.10.0", "6.0.0"):
            self.assertIn(f"wkm_version_at_least {version} 5 4", self_test)
        for version in ("5.3.999", "4.999.999"):
            self.assertIn(f"! wkm_version_at_least {version} 5 4", self_test)

    def test_shared_host_changes_are_namespaced_and_fail_closed(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        service = (
            ROOT / "deploy/el9/wechat-memory-certbot-renew.service"
        ).read_text(encoding="utf-8")
        timer = (
            ROOT / "deploy/el9/wechat-memory-certbot-renew.timer"
        ).read_text(encoding="utf-8")

        self.assertIn('CERTBOT_ROOT="/opt/wechat-memory-license-certbot"', script)
        self.assertIn("wkm_assert_owned_nginx_file", script)
        self.assertIn("wkm_assert_no_foreign_nginx_conflict", script)
        self.assertIn("wkm_assert_foreign_nginx_unchanged", script)
        self.assertIn("verify_local_acme_route", script)
        certbot_call = script.index('"${CERTBOT_ROOT}/bin/certbot" certonly')
        self.assertLess(script.rindex("verify_local_acme_route", 0, certbot_call), certbot_call)
        self.assertLess(
            script.index("trap on_deploy_exit EXIT"),
            script.index("\nconfigure_firewall\n"),
        )
        self.assertIn("restore_host_prerequisites", script)
        self.assertIn('--zone="${FIREWALL_ZONE}" --add-service=http', script)
        self.assertIn('--zone="${FIREWALL_ZONE}" --remove-service=http', script)
        self.assertIn('--add-port="${PUBLIC_PORT}/tcp"', script)
        self.assertIn('--remove-port="${PUBLIC_PORT}/tcp"', script)
        self.assertIn("wechat-memory-certbot-renew.timer", script)
        self.assertNotIn("/etc/systemd/system/certbot-renew.", script)
        self.assertNotIn("firewall-cmd --reload", script)
        self.assertNotIn("systemctl enable --now firewalld", script)
        self.assertNotIn("semanage port -m", script)
        self.assertIn("selinux-port-safety.sh", script)
        selinux_safety = (
            ROOT / "deploy/el9/selinux-port-safety.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('case "${selinux_mode}"', selinux_safety)
        self.assertIn("Disabled)", selinux_safety)
        self.assertIn("wkm_selinux_types_for_tcp_port", selinux_safety)
        self.assertNotIn("semanage port -m", selinux_safety)
        self.assertNotIn("for (index =", selinux_safety)
        self.assertIn("--cert-name 104.244.90.172", service)
        self.assertIn("/opt/wechat-memory-license-certbot/bin/certbot", service)
        self.assertIn("ReadWritePaths=/etc/letsencrypt", service)
        self.assertIn("Unit=wechat-memory-certbot-renew.service", timer)

        manual = (ROOT / "docs/MANUAL_DEPLOY_EL9_IP_CERT.md").read_text(
            encoding="utf-8"
        )
        one_click = (ROOT / "docs/ONE_CLICK_DEPLOY_AND_ADMIN.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("本机成功、外部仍是 `connection refused`", manual)
        self.assertIn("deploy/el9/nginx-production-tls.conf", manual)
        self.assertNotIn(
            "install -o root -g root -m 0644 deploy/el9/nginx-production.conf",
            manual,
        )
        self.assertIn("外部探针未成功时禁止运行正式 Certbot", one_click)

    def test_explicit_nginx_retirement_is_transactional_and_one_time(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        wrapper = (ROOT / "deploy/el9/deploy-from-data.sh").read_text(
            encoding="utf-8"
        )
        helper = (ROOT / "deploy/el9/shared-host-safety.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("--retire-nginx-conf", wrapper)
        self.assertIn(
            'arguments+=(--retire-nginx-conf "${RETIRE_NGINX_CONF}")', wrapper
        )
        self.assertIn("--retire-nginx-conf", script)
        validation = script.index("wkm_validate_retire_nginx_conf")
        dnf_install = script.index("dnf install -y")
        trap_install = script.index("trap on_deploy_exit EXIT")
        pre_digest = script.index('NGINX_FOREIGN_PRE_RETIRE_BASELINE="$(')
        retirement = script.index('wkm_retire_nginx_conf "${APPLICATION_TRANSACTION_DIR}"')
        checkpoint = script.index("wkm_deploy_checkpoint after_nginx_retirement")
        conflict = script.index("wkm_assert_no_foreign_nginx_conflict", retirement)
        firewall = script.index("\nconfigure_firewall\n")
        finalize = script.index("wkm_finalize_retired_nginx_conf")
        commit = script.index("DEPLOY_COMPLETED=1")

        self.assertLess(validation, dnf_install)
        self.assertLess(trap_install, pre_digest)
        self.assertLess(pre_digest, retirement)
        self.assertLess(retirement, checkpoint)
        self.assertLess(checkpoint, conflict)
        self.assertLess(conflict, firewall)
        self.assertLess(finalize, commit)
        for token in (
            "wkm_tx_capture_file",
            "WKM_RETIRED_NGINX_SHA256",
            "wkm_write_retired_nginx_record committed",
            "wkm_restore_retired_nginx_conf",
            '[[ -e "${original_path}" || -L "${original_path}" ]]',
            "未声明目标 IP",
        ):
            self.assertIn(token, helper)

    def test_owned_legacy_endpoint_is_rebuilt_not_rejected(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "将已确认归属 WeChatMemory 的旧公网 IP 配置迁移到 ${PUBLIC_IP}",
            script,
        )
        self.assertNotIn(
            "已有 WeChatMemory Nginx 配置与当前 --public-ip/ACME webroot 不一致",
            script,
        )
        backup = script.index("backup_nginx_config", script.index("ACME_ROUTE_VERIFIED"))
        install = script.index(
            'install -o root -g root -m 0644 "${BOOTSTRAP}.tmp" "${BOOTSTRAP}"'
        )
        self.assertLess(backup, install)

    def test_signing_key_metadata_is_gated_before_host_mutation(self) -> None:
        script = (ROOT / "deploy/el9/one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        installer = (ROOT / "deploy/el9/install-release.sh").read_text(
            encoding="utf-8"
        )
        wrapper = (ROOT / "deploy/el9/deploy-from-data.sh").read_text(
            encoding="utf-8"
        )
        staged_gate = script.index(
            'wkm_assert_staged_signing_key "${SIGNING_KEY}"'
        )
        package_install = script.index("dnf install -y")
        self.assertLess(staged_gate, package_install)
        self.assertIn("signing-key-safety.sh", script)
        self.assertIn(
            'wkm_assert_staged_signing_key "${STAGED_KEY}"', installer
        )
        self.assertLess(
            installer.index('wkm_assert_staged_signing_key "${STAGED_KEY}"'),
            installer.index("useradd --system"),
        )
        self.assertIn(
            '[[ -e "${KEY_PATH}" || -L "${KEY_PATH}" ]]', installer
        )
        self.assertIn(
            '[[ -e "${LEGACY_KEY_PATH}" || -L "${LEGACY_KEY_PATH}" ]]',
            installer,
        )
        self.assertGreaterEqual(
            installer.count('wkm_assert_installed_signing_key "${KEY_PATH}"'), 2
        )
        self.assertIn('--signing-key "${SIGNING_KEY}"', wrapper)
        self.assertNotIn('if [[ -f "${SIGNING_KEY}" ]]', wrapper)


if __name__ == "__main__":
    unittest.main()
