#!/usr/bin/env python3
"""Generate and verify the server runtime SPDX SBOM without network access."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


ROOT = Path(__file__).resolve().parents[1]
SBOM_PATH = ROOT / "sbom" / "server-runtime.spdx.json"
LICENSE_ID = "LicenseRef-WeChatMemory-License-Server-Proprietary"
_CANONICAL_NAME = re.compile(r"[-_.]+")


class SbomError(RuntimeError):
    """Fail-closed inventory or validation error."""


def _load_packaging() -> tuple[Any, Callable[[], Mapping[str, str]]]:
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
    except ImportError:
        # pip is present in every deployment virtual environment and vendors the
        # PEP 508 parser.  This fallback avoids adding a runtime-only dependency.
        try:
            from pip._vendor.packaging.markers import default_environment
            from pip._vendor.packaging.requirements import Requirement
        except ImportError as exc:  # pragma: no cover - only malformed venvs
            raise SbomError(
                "PEP 508 parser unavailable; install pip in the candidate venv"
            ) from exc
    return Requirement, default_environment


def canonical_name(value: str) -> str:
    return _CANONICAL_NAME.sub("-", value).lower()


def _spdx_package_id(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]+", "-", canonical_name(name)).strip("-")
    if not safe:
        raise SbomError(f"cannot create SPDX id for package: {name!r}")
    return f"SPDXRef-Package-{safe}"


def _all(metadata: Any, field: str) -> list[str]:
    get_all = getattr(metadata, "get_all", None)
    if callable(get_all):
        return [str(value) for value in (get_all(field) or []) if value]
    value = metadata.get(field) if hasattr(metadata, "get") else None
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return [str(value)] if value else []


def _one(metadata: Any, field: str) -> str | None:
    values = _all(metadata, field)
    return values[0].strip() if values and values[0].strip() else None


def _declared_license(metadata: Any) -> str:
    expression = _one(metadata, "License-Expression")
    if expression:
        return expression
    raw = (_one(metadata, "License") or "").strip().lower()
    known = {
        "zpl 2.1": "ZPL-2.1",
        "zope public license 2.1": "ZPL-2.1",
    }
    return known.get(raw, "NOASSERTION")


def _homepage(metadata: Any) -> str | None:
    home = _one(metadata, "Home-page")
    if home:
        return home
    project_urls = _all(metadata, "Project-URL")
    preferred = ("homepage", "source", "source code", "documentation")
    parsed: dict[str, str] = {}
    for value in project_urls:
        if "," not in value:
            continue
        label, url = value.split(",", 1)
        if url.strip().startswith(("https://", "http://")):
            parsed[label.strip().lower()] = url.strip()
    for label in preferred:
        if label in parsed:
            return parsed[label]
    return next(iter(parsed.values()), None)


def _created_from_epoch(epoch: int) -> str:
    if epoch < 0:
        raise SbomError("SOURCE_DATE_EPOCH must be non-negative")
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_created() -> str:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        epoch = int(raw)
    except ValueError as exc:
        raise SbomError("SOURCE_DATE_EPOCH must be an integer") from exc
    return _created_from_epoch(epoch)


def _load_project(root: Path) -> tuple[str, str, list[str]]:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
    except (OSError, UnicodeError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise SbomError(f"cannot read project metadata: {exc}") from exc
    name = str(project.get("name", "")).strip()
    version = str(project.get("version", "")).strip()
    dependencies = project.get("dependencies", [])
    if not name or not version or not isinstance(dependencies, list):
        raise SbomError("pyproject project name, version or dependencies are invalid")
    return name, version, [str(item) for item in dependencies]


def _runtime_requirement(
    requirement: Any,
    environment: Mapping[str, str],
    *,
    active_extras: set[str] | None = None,
) -> bool:
    if requirement.marker is None:
        return True
    for extra in active_extras or {""}:
        marker_environment = dict(environment)
        marker_environment["extra"] = extra
        if requirement.marker.evaluate(marker_environment):
            return True
    return False


def build_document(
    root: Path = ROOT,
    *,
    created: str | None = None,
    distribution_lookup: Callable[[str], Any] = importlib.metadata.distribution,
    marker_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a deterministic SPDX 2.3 document from the installed environment."""

    Requirement, default_environment = _load_packaging()
    environment = dict(marker_environment or default_environment())
    app_name, app_version, direct_requirements = _load_project(root)
    app_id = _spdx_package_id(app_name)

    records: dict[str, dict[str, Any]] = {}
    relationships: set[tuple[str, str, str]] = set()
    scopes: dict[str, str] = {}
    pending: list[tuple[Any, str, str]] = []
    for raw in direct_requirements:
        try:
            requirement = Requirement(raw)
        except Exception as exc:
            raise SbomError(f"invalid direct requirement {raw!r}: {exc}") from exc
        if _runtime_requirement(requirement, environment):
            pending.append((requirement, app_id, "direct"))

    expanded_extras: dict[str, set[str]] = {}
    while pending:
        requirement, parent_id, scope = pending.pop(0)
        name_key = canonical_name(requirement.name)
        try:
            distribution = distribution_lookup(requirement.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise SbomError(
                f"required distribution is not installed: {requirement.name}"
            ) from exc
        version = str(distribution.version)
        if requirement.specifier and not requirement.specifier.contains(
            version, prereleases=True
        ):
            raise SbomError(
                f"installed {requirement.name}=={version} does not satisfy "
                f"{requirement.specifier}"
            )

        metadata = distribution.metadata
        canonical = canonical_name(_one(metadata, "Name") or requirement.name)
        if canonical != name_key:
            raise SbomError(
                f"distribution lookup mismatch: requested {requirement.name}, got {canonical}"
            )
        package_id = _spdx_package_id(canonical)
        relationships.add((parent_id, "DEPENDS_ON", package_id))
        if scopes.get(canonical) != "direct":
            scopes[canonical] = scope

        if canonical not in records:
            license_metadata = {
                "license": _all(metadata, "License"),
                "license_expression": _all(metadata, "License-Expression"),
                "license_classifiers": [
                    item
                    for item in _all(metadata, "Classifier")
                    if item.startswith("License ::")
                ],
                "license_files": _all(metadata, "License-File"),
            }
            record: dict[str, Any] = {
                "SPDXID": package_id,
                "name": _one(metadata, "Name") or requirement.name,
                "versionInfo": version,
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": _declared_license(metadata),
                "copyrightText": "NOASSERTION",
                "primaryPackagePurpose": "LIBRARY",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": f"pkg:pypi/{canonical}@{version}",
                    }
                ],
                "comment": "",  # finalized after direct/transitive scope is known
            }
            homepage = _homepage(metadata)
            if homepage:
                record["homepage"] = homepage
            record["_license_metadata"] = license_metadata
            records[canonical] = record

        requested_extras = set(requirement.extras) or {""}
        previous_extras = expanded_extras.setdefault(canonical, set())
        new_extras = requested_extras - previous_extras
        if not new_extras:
            continue
        previous_extras.update(new_extras)
        for raw in distribution.requires or []:
            try:
                child = Requirement(raw)
            except Exception as exc:
                raise SbomError(
                    f"invalid dependency metadata for {canonical}: {raw!r}: {exc}"
                ) from exc
            if _runtime_requirement(child, environment, active_extras=new_extras):
                pending.append((child, package_id, "transitive"))

    packages: list[dict[str, Any]] = [
        {
            "SPDXID": app_id,
            "name": app_name,
            "versionInfo": app_version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": LICENSE_ID,
            "licenseDeclared": LICENSE_ID,
            "copyrightText": "Copyright (c) 2026 WeChatMemory. All rights reserved.",
            "primaryPackagePurpose": "APPLICATION",
            "comment": json.dumps(
                {"dependency_scope": "application"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    ]
    for canonical in sorted(records):
        record = records[canonical]
        license_metadata = record.pop("_license_metadata")
        record["comment"] = json.dumps(
            {
                "dependency_scope": scopes[canonical],
                "license_metadata": license_metadata,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        packages.append(record)

    relationship_rows = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": app_id,
        }
    ]
    relationship_rows.extend(
        {
            "spdxElementId": source,
            "relationshipType": relation,
            "relatedSpdxElement": target,
        }
        for source, relation, target in sorted(relationships)
    )
    digest_input = json.dumps(
        {"packages": packages, "relationships": relationship_rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_digest = hashlib.sha256(digest_input).hexdigest()
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    document: dict[str, Any] = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{app_name}-{app_version}-runtime",
        "documentNamespace": (
            "https://github.com/gladlyknow/WeChatMemory-LicenseServer/"
            f"spdx/{app_version}/{content_digest}"
        ),
        "creationInfo": {
            "created": created or _default_created(),
            "creators": [
                "Organization: WeChatMemory",
                "Tool: scripts/generate_sbom.py",
            ],
        },
        "documentDescribes": [app_id],
        "packages": packages,
        "relationships": relationship_rows,
        "hasExtractedLicensingInfos": [
            {
                "licenseId": LICENSE_ID,
                "name": "WeChatMemory License Server Proprietary Software License",
                "extractedText": license_text,
                "comment": "Engineering release baseline; human legal review remains required.",
            }
        ],
    }
    errors = validate_document(document)
    if errors:
        raise SbomError("generated invalid SPDX document: " + "; ".join(errors))
    return document


def validate_document(document: Any) -> list[str]:
    """Validate the fail-closed invariants needed by the local release gate."""

    errors: list[str] = []
    if not isinstance(document, dict):
        return ["document must be a JSON object"]
    for key, expected in (
        ("spdxVersion", "SPDX-2.3"),
        ("dataLicense", "CC0-1.0"),
        ("SPDXID", "SPDXRef-DOCUMENT"),
    ):
        if document.get(key) != expected:
            errors.append(f"{key} must be {expected}")
    namespace = document.get("documentNamespace")
    if not isinstance(namespace, str) or not namespace.startswith("https://"):
        errors.append("documentNamespace must be an https URL")
    creation = document.get("creationInfo")
    if not isinstance(creation, dict) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", str(creation.get("created", ""))
    ):
        errors.append("creationInfo.created must be an SPDX UTC timestamp")

    packages = document.get("packages")
    if not isinstance(packages, list) or len(packages) < 2:
        errors.append("packages must include the application and runtime dependencies")
        return errors
    identifiers: set[str] = set()
    application_ids: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            errors.append("every package must be an object")
            continue
        package_id = package.get("SPDXID")
        if not isinstance(package_id, str) or not package_id.startswith("SPDXRef-"):
            errors.append("every package must have an SPDXRef id")
            continue
        if package_id in identifiers:
            errors.append(f"duplicate package SPDX id: {package_id}")
        identifiers.add(package_id)
        if not package.get("name") or not package.get("versionInfo"):
            errors.append(f"package {package_id} is missing name/versionInfo")
        if package.get("filesAnalyzed") is not False:
            errors.append(f"package {package_id} must declare filesAnalyzed=false")
        purpose = package.get("primaryPackagePurpose")
        if purpose == "APPLICATION":
            application_ids.add(package_id)
        elif purpose != "LIBRARY":
            errors.append(f"package {package_id} has invalid primaryPackagePurpose")
        try:
            comment = json.loads(str(package.get("comment", "")))
        except json.JSONDecodeError:
            errors.append(f"package {package_id} comment is not canonical JSON")
            continue
        scope = comment.get("dependency_scope")
        if purpose == "APPLICATION":
            if scope != "application":
                errors.append(f"application {package_id} has invalid dependency scope")
        else:
            if scope not in {"direct", "transitive"}:
                errors.append(f"dependency {package_id} has invalid dependency scope")
            metadata = comment.get("license_metadata")
            required = {
                "license",
                "license_expression",
                "license_classifiers",
                "license_files",
            }
            if not isinstance(metadata, dict) or set(metadata) != required:
                errors.append(f"dependency {package_id} lacks complete license metadata")
            refs = package.get("externalRefs")
            if not isinstance(refs, list) or not any(
                isinstance(ref, dict)
                and ref.get("referenceType") == "purl"
                and str(ref.get("referenceLocator", "")).startswith("pkg:pypi/")
                for ref in refs
            ):
                errors.append(f"dependency {package_id} lacks a PyPI purl")

    if len(application_ids) != 1:
        errors.append("exactly one application package is required")
    describes = document.get("documentDescribes")
    if set(describes or []) != application_ids:
        errors.append("documentDescribes must identify the application package")

    relationships = document.get("relationships")
    if not isinstance(relationships, list):
        errors.append("relationships must be a list")
        return errors
    graph: dict[str, set[str]] = {}
    has_describes = False
    for relation in relationships:
        if not isinstance(relation, dict):
            errors.append("every relationship must be an object")
            continue
        source = relation.get("spdxElementId")
        target = relation.get("relatedSpdxElement")
        kind = relation.get("relationshipType")
        if source == "SPDXRef-DOCUMENT" and kind == "DESCRIBES" and target in application_ids:
            has_describes = True
            continue
        if kind != "DEPENDS_ON":
            errors.append(f"unexpected relationship type: {kind}")
            continue
        if source not in identifiers or target not in identifiers:
            errors.append(f"relationship references unknown package: {source} -> {target}")
            continue
        graph.setdefault(str(source), set()).add(str(target))
    if not has_describes:
        errors.append("missing DOCUMENT DESCRIBES relationship")
    if application_ids:
        reachable = set(application_ids)
        pending = list(application_ids)
        while pending:
            for child in graph.get(pending.pop(), set()):
                if child not in reachable:
                    reachable.add(child)
                    pending.append(child)
        missing = identifiers - reachable
        if missing:
            errors.append("unreachable dependency packages: " + ", ".join(sorted(missing)))

    extracted = document.get("hasExtractedLicensingInfos")
    if not isinstance(extracted, list) or not any(
        isinstance(item, dict)
        and item.get("licenseId") == LICENSE_ID
        and bool(item.get("extractedText"))
        for item in extracted
    ):
        errors.append("missing extracted proprietary license text")
    return errors


def validate_third_party_notices(document: dict[str, Any], path: Path) -> list[str]:
    """Require the human notice table to track every exact runtime package."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"cannot read third-party notices: {exc}"]
    rows: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"Component", "---"}:
            continue
        if set(cells[0]) == {"-"}:
            continue
        rows[canonical_name(cells[0])] = cells[1]
    errors: list[str] = []
    dependencies = {
        canonical_name(str(package["name"])): str(package["versionInfo"])
        for package in document.get("packages", [])
        if package.get("primaryPackagePurpose") == "LIBRARY"
    }
    for name, version in sorted(dependencies.items()):
        if name not in rows:
            errors.append(f"THIRD_PARTY_NOTICES.md is missing {name}=={version}")
        elif rows[name] != version:
            errors.append(
                f"THIRD_PARTY_NOTICES.md records {name}=={rows[name]}, expected {version}"
            )
    unexpected = sorted(set(rows) - set(dependencies))
    if unexpected:
        errors.append(
            "THIRD_PARTY_NOTICES.md has packages outside the runtime SBOM: "
            + ", ".join(unexpected)
        )
    return errors


def _serialize(document: dict[str, Any]) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _load_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SbomError(f"cannot read SPDX document {path}: {exc}") from exc
    errors = validate_document(document)
    if errors:
        raise SbomError("invalid SPDX document: " + "; ".join(errors))
    return document


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or validate the offline SPDX runtime inventory."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--output", type=Path, help="write a freshly generated SBOM")
    action.add_argument(
        "--check",
        type=Path,
        help="compare the installed environment with an approved SBOM",
    )
    action.add_argument(
        "--validate", type=Path, help="validate an existing SBOM without inventorying"
    )
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        help="override SOURCE_DATE_EPOCH for deterministic creationInfo",
    )
    args = parser.parse_args(argv)
    try:
        if args.validate:
            document = _load_document(args.validate)
            notice_errors = validate_third_party_notices(
                document, ROOT / "THIRD_PARTY_NOTICES.md"
            )
            if notice_errors:
                raise SbomError("; ".join(notice_errors))
            print(f"SPDX validation: ok ({args.validate})")
            return 0
        if args.output:
            created = (
                _created_from_epoch(args.source_date_epoch)
                if args.source_date_epoch is not None
                else None
            )
            document = build_document(created=created)
            _write_atomic(args.output, _serialize(document))
            print(f"SPDX generated: {args.output}")
            return 0

        expected = _load_document(args.check)
        notice_errors = validate_third_party_notices(
            expected, ROOT / "THIRD_PARTY_NOTICES.md"
        )
        if notice_errors:
            raise SbomError("; ".join(notice_errors))
        created = str(expected["creationInfo"]["created"])
        actual = build_document(created=created)
        expected_bytes = _serialize(expected)
        actual_bytes = _serialize(actual)
        if actual_bytes != expected_bytes:
            expected_packages = {
                (item["name"], item["versionInfo"]) for item in expected["packages"]
            }
            actual_packages = {
                (item["name"], item["versionInfo"]) for item in actual["packages"]
            }
            removed = sorted(expected_packages - actual_packages)
            added = sorted(actual_packages - expected_packages)
            detail = []
            if removed:
                detail.append(f"expected-only={removed}")
            if added:
                detail.append(f"candidate-only={added}")
            if not detail:
                detail.append("metadata or dependency relationships changed")
            raise SbomError("candidate SBOM differs from approved baseline: " + "; ".join(detail))
        print(f"SPDX environment check: ok ({args.check})")
        return 0
    except (OSError, UnicodeError, SbomError) as exc:
        print(f"SBOM error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
