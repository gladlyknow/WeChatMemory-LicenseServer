from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_documentation.py"
SPEC = importlib.util.spec_from_file_location("check_documentation", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class DocumentationContractTests(unittest.TestCase):
    def test_repository_documentation_contract(self) -> None:
        self.assertEqual(CHECKER.check_repository(ROOT), [])

    def test_relative_link_checker_rejects_missing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            for relative in CHECKER.CORE_DOCUMENTS:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("placeholder\n", encoding="utf-8")
            for relative in CHECKER.BASELINE_DOCUMENTS:
                path = root / relative
                path.write_text(
                    "\n".join(
                        (
                            CHECKER.SOURCE_DIRECTORY,
                            CHECKER.PUBLIC_ENDPOINT,
                            CHECKER.ADMIN_EMAIL,
                        )
                    ),
                    encoding="utf-8",
                )
            (root / "README.md").write_text(
                (root / "README.md").read_text(encoding="utf-8")
                + "\n[missing](docs/not-present.md)\n"
                + "```bash\ncommand --device <unsafe-id>\n```\n",
                encoding="utf-8",
            )
            for relative, values in {
                "deploy/el9/deploy-from-data.sh": (
                    f'SOURCE_DIR="{CHECKER.SOURCE_DIRECTORY}"',
                    f'ADMIN_EMAIL="{CHECKER.ADMIN_EMAIL}"',
                    'PUBLIC_IP="104.224.90.172"',
                    'PUBLIC_PORT="9000"',
                ),
                "deploy/el9/one-click-deploy.sh": (
                    f'SOURCE_DIR="{CHECKER.SOURCE_DIRECTORY}"',
                    f'EMAIL="{CHECKER.ADMIN_EMAIL}"',
                    'PUBLIC_IP="104.224.90.172"',
                    'PUBLIC_PORT="9000"',
                ),
            }.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("\n".join(values), encoding="utf-8")
            errors = CHECKER.check_repository(root)
            self.assertTrue(any("broken relative link" in item for item in errors))
            self.assertTrue(
                any("unsafe angle-bracket placeholder" in item for item in errors)
            )

    def test_release_installer_enforces_documentation_contract(self) -> None:
        installer = (ROOT / "deploy" / "el9" / "install-release.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("REQUIRED_RELEASE_DOCUMENTS", installer)
        self.assertIn("docs/DOCUMENTATION_POLICY.md", installer)
        self.assertIn("docs/SERVER_OPERATIONS_RUNBOOK.md", installer)
        self.assertIn('scripts/check_documentation.py"', installer)
        self.assertLess(
            installer.index('scripts/check_documentation.py"'),
            installer.index('BACKUP_PATH="'),
        )
        self.assertLess(
            installer.index('BACKUP_PATH="'),
            installer.index('pip" install --upgrade pip'),
        )

        one_click = (ROOT / "deploy" / "el9" / "one-click-deploy.sh").read_text(
            encoding="utf-8"
        )
        documentation_gate = one_click.index(
            'python3.11 "${SOURCE_DIR}/scripts/check_documentation.py"'
        )
        bootstrap = one_click.index("BOOTSTRAP=/etc/nginx/conf.d/")
        exit_trap = one_click.index("trap on_deploy_exit EXIT")
        bootstrap_install = one_click.index(
            'install -o root -g root -m 0644 "${BOOTSTRAP}.tmp" "${BOOTSTRAP}"'
        )
        self.assertLess(documentation_gate, bootstrap)
        self.assertLess(bootstrap, exit_trap)
        self.assertLess(exit_trap, bootstrap_install)
        self.assertIn("DEPLOY_COMPLETED=1", one_click)
        self.assertIn("restore_nginx_config", one_click)


if __name__ == "__main__":
    unittest.main()
