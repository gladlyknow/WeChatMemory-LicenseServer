from __future__ import annotations

import subprocess
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

    def test_release_dependencies_are_isolated_and_runtime_uses_current(self) -> None:
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
        self.assertIn("PYTHONWARNINGS=error::SyntaxWarning", installer)
        self.assertIn("bash -n deploy/el9/*.sh deploy/el9/wkm-license-admin", installer)
        self.assertIn("bash deploy/el9/test-deployment-transaction.sh", installer)
        self.assertNotIn('"${APP_ROOT}/venv/bin/pip"', installer)
        self.assertNotIn('"${APP_ROOT}/venv/bin/python"', installer)
        self.assertIn(
            "ExecStart=/opt/wechat-memory-license/current/.venv/bin/python",
            service,
        )
        self.assertIn(
            "PYTHON=/opt/wechat-memory-license/current/.venv/bin/python", admin
        )

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
        self.assertLess(environment_install, migration)
        self.assertLess(migration, switch)
        self.assertLess(switch, local_acceptance)
        self.assertLess(local_acceptance, commit)
        self.assertIn("rollback_install_runtime", installer)
        self.assertIn("wkm_tx_restore_link", installer)
        self.assertIn("wkm_tx_restore_file", installer)
        self.assertIn("不会在服务可能产生新写入后自动降级数据库", installer)

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
            '"https://${PUBLIC_IP}:${PUBLIC_PORT}/readyz"'
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

    def test_certbot_version_has_format_and_numeric_minimum_gates(self) -> None:
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
        self.assertIn("--cert-name 104.224.90.172", service)
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
        self.assertIn("外部探针未成功时禁止运行正式 Certbot", one_click)


if __name__ == "__main__":
    unittest.main()
