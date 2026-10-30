from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_sbom.py"
SPEC = importlib.util.spec_from_file_location("generate_sbom", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SBOM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SBOM)


class FakeDistribution:
    def __init__(
        self,
        name: str,
        version: str,
        *,
        requires: list[str] | None = None,
        license_expression: str | None = None,
        classifiers: list[str] | None = None,
        license_files: list[str] | None = None,
    ) -> None:
        metadata = Message()
        metadata["Name"] = name
        metadata["Version"] = version
        metadata["Project-URL"] = f"Source, https://example.invalid/{name}"
        if license_expression:
            metadata["License-Expression"] = license_expression
        for classifier in classifiers or []:
            metadata["Classifier"] = classifier
        for license_file in license_files or []:
            metadata["License-File"] = license_file
        self.metadata = metadata
        self.version = version
        self.requires = requires or []


class SbomTests(unittest.TestCase):
    def _root(self, temporary: str, dependency: str = "alpha>=1,<2") -> Path:
        root = Path(temporary)
        (root / "pyproject.toml").write_text(
            "\n".join(
                (
                    "[project]",
                    'name = "sample-server"',
                    'version = "1.2.3"',
                    f'dependencies = ["{dependency}"]',
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "LICENSE").write_text("sample proprietary license\n", encoding="utf-8")
        return root

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            "implementation_name": "cpython",
            "implementation_version": "3.12.0",
            "os_name": "posix",
            "platform_machine": "x86_64",
            "platform_python_implementation": "CPython",
            "platform_release": "test",
            "platform_system": "Linux",
            "platform_version": "test",
            "python_full_version": "3.12.0",
            "python_version": "3.12",
            "sys_platform": "linux",
            "extra": "",
        }

    def test_build_records_exact_direct_and_transitive_dependencies(self) -> None:
        distributions = {
            "alpha": FakeDistribution(
                "Alpha",
                "1.4.0",
                requires=[
                    "beta==2.0",
                    'ignored-extra==9; extra == "docs"',
                    'ignored-platform==9; sys_platform == "win32"',
                ],
                license_expression="BSD-3-Clause",
                license_files=["LICENSE.txt"],
            ),
            "beta": FakeDistribution(
                "beta",
                "2.0",
                license_expression="MIT",
                classifiers=["License :: OSI Approved :: MIT License"],
            ),
        }

        def lookup(name: str) -> FakeDistribution:
            try:
                return distributions[SBOM.canonical_name(name)]
            except KeyError as exc:
                raise importlib.metadata.PackageNotFoundError(name) from exc

        with tempfile.TemporaryDirectory() as temporary:
            document = SBOM.build_document(
                self._root(temporary),
                created="2026-01-02T03:04:05Z",
                distribution_lookup=lookup,
                marker_environment=self._environment(),
            )

        self.assertEqual(SBOM.validate_document(document), [])
        packages = {item["name"].lower(): item for item in document["packages"]}
        self.assertEqual(set(packages), {"sample-server", "alpha", "beta"})
        alpha_comment = json.loads(packages["alpha"]["comment"])
        beta_comment = json.loads(packages["beta"]["comment"])
        self.assertEqual(alpha_comment["dependency_scope"], "direct")
        self.assertEqual(beta_comment["dependency_scope"], "transitive")
        self.assertEqual(packages["alpha"]["versionInfo"], "1.4.0")
        self.assertEqual(packages["beta"]["licenseDeclared"], "MIT")
        self.assertEqual(
            alpha_comment["license_metadata"]["license_files"], ["LICENSE.txt"]
        )
        relationships = {
            (
                item["spdxElementId"],
                item["relationshipType"],
                item["relatedSpdxElement"],
            )
            for item in document["relationships"]
        }
        self.assertIn(
            (
                "SPDXRef-Package-sample-server",
                "DEPENDS_ON",
                "SPDXRef-Package-alpha",
            ),
            relationships,
        )
        self.assertIn(
            ("SPDXRef-Package-alpha", "DEPENDS_ON", "SPDXRef-Package-beta"),
            relationships,
        )

    def test_build_is_byte_deterministic_for_same_environment(self) -> None:
        distribution = FakeDistribution("alpha", "1.1.0", license_expression="MIT")
        with tempfile.TemporaryDirectory() as temporary:
            root = self._root(temporary)
            first = SBOM.build_document(
                root,
                created="2026-01-02T03:04:05Z",
                distribution_lookup=lambda _: distribution,
                marker_environment=self._environment(),
            )
            second = SBOM.build_document(
                root,
                created="2026-01-02T03:04:05Z",
                distribution_lookup=lambda _: distribution,
                marker_environment=self._environment(),
            )
        self.assertEqual(SBOM._serialize(first), SBOM._serialize(second))

    def test_selected_runtime_extra_is_followed(self) -> None:
        distributions = {
            "alpha": FakeDistribution(
                "alpha",
                "1.1.0",
                requires=['feature-lib==4; extra == "feature"'],
            ),
            "feature-lib": FakeDistribution("feature-lib", "4"),
        }

        def lookup(name: str) -> FakeDistribution:
            return distributions[SBOM.canonical_name(name)]

        with tempfile.TemporaryDirectory() as temporary:
            document = SBOM.build_document(
                self._root(temporary, "alpha[feature]>=1,<2"),
                distribution_lookup=lookup,
                marker_environment=self._environment(),
            )
        names = {item["name"] for item in document["packages"]}
        self.assertEqual(names, {"sample-server", "alpha", "feature-lib"})

    def test_unsatisfied_installed_version_fails_closed(self) -> None:
        distribution = FakeDistribution("alpha", "2.5.0")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SBOM.SbomError, "does not satisfy"):
                SBOM.build_document(
                    self._root(temporary),
                    distribution_lookup=lambda _: distribution,
                    marker_environment=self._environment(),
                )

    def test_missing_distribution_fails_closed(self) -> None:
        def missing(name: str) -> FakeDistribution:
            raise importlib.metadata.PackageNotFoundError(name)

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(SBOM.SbomError, "not installed"):
                SBOM.build_document(
                    self._root(temporary),
                    distribution_lookup=missing,
                    marker_environment=self._environment(),
                )

    def test_validator_rejects_unreachable_dependency(self) -> None:
        document = json.loads(
            (ROOT / "sbom" / "server-runtime.spdx.json").read_text(encoding="utf-8")
        )
        document["relationships"] = [
            item
            for item in document["relationships"]
            if item.get("relatedSpdxElement") != "SPDXRef-Package-waitress"
        ]
        errors = SBOM.validate_document(document)
        self.assertTrue(any("unreachable dependency" in item for item in errors))

    def test_committed_sbom_is_valid_and_matches_candidate_environment(self) -> None:
        path = ROOT / "sbom" / "server-runtime.spdx.json"
        expected = SBOM._load_document(path)
        self.assertEqual(
            SBOM.validate_third_party_notices(
                expected, ROOT / "THIRD_PARTY_NOTICES.md"
            ),
            [],
        )
        actual = SBOM.build_document(created=expected["creationInfo"]["created"])
        self.assertEqual(SBOM._serialize(actual), SBOM._serialize(expected))

    def test_runtime_dependencies_are_exactly_pinned_before_install(self) -> None:
        locked: dict[str, str] = {}
        for raw in (ROOT / "requirements.lock").read_text(
            encoding="utf-8"
        ).splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self.assertEqual(line.count("=="), 1, line)
            name, version = line.split("==", 1)
            locked[SBOM.canonical_name(name)] = version

        document = SBOM._load_document(ROOT / "sbom" / "server-runtime.spdx.json")
        runtime = {
            SBOM.canonical_name(str(item["name"])): str(item["versionInfo"])
            for item in document["packages"]
            if item.get("primaryPackagePurpose") == "LIBRARY"
        }
        self.assertEqual(
            {name: locked.get(name) for name in runtime},
            runtime,
        )

    def test_notice_validator_rejects_version_drift(self) -> None:
        document = SBOM._load_document(ROOT / "sbom" / "server-runtime.spdx.json")
        with tempfile.TemporaryDirectory() as temporary:
            notices = Path(temporary) / "THIRD_PARTY_NOTICES.md"
            notices.write_text("| Component | Exact accepted version |\n| Flask | 0 |\n")
            errors = SBOM.validate_third_party_notices(document, notices)
        self.assertTrue(any("expected 3.1.3" in item for item in errors))
        self.assertTrue(any("is missing waitress" in item for item in errors))

    def test_validation_command_does_not_inventory_environment(self) -> None:
        path = ROOT / "sbom" / "server-runtime.spdx.json"
        self.assertEqual(SBOM.main(["--validate", str(path)]), 0)


if __name__ == "__main__":
    unittest.main()
