#!/usr/bin/env python3
"""Validate the local documentation contract without network access."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
CORE_DOCUMENTS = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "docs/README.md",
    "docs/DOCUMENTATION_POLICY.md",
    "docs/ACCEPTANCE_CRITERIA.md",
    "docs/LEGAL_AND_SBOM_RELEASE_GATE.md",
    "docs/SERVER_OPERATIONS_RUNBOOK.md",
    "docs/LICENSE_SERVER_ARCHITECTURE.md",
    "docs/ONE_CLICK_DEPLOY_AND_ADMIN.md",
    "docs/MANUAL_DEPLOY_EL9_IP_CERT.md",
    "docs/SIGNING_KEY_OPERATIONS.md",
    "docs/DEPLOYMENT_104.244.90.172.md",
    "sbom/server-runtime.spdx.json",
    "scripts/generate_sbom.py",
    "deploy/el9/signing-key-safety.sh",
)
BASELINE_DOCUMENTS = (
    "README.md",
    "docs/README.md",
    "docs/SERVER_OPERATIONS_RUNBOOK.md",
    "docs/ONE_CLICK_DEPLOY_AND_ADMIN.md",
)
SOURCE_DIRECTORY = "/data/WeChatMemory-LicenseServer"
PUBLIC_ENDPOINT = "http://104.244.90.172:9000"
RETIRED_PUBLIC_IP = "104.224.90.172"
ADMIN_EMAIL = "gladlygrady@tutamail.com"
CLIENT_VERSION = "0.3.5"
RETIRED_CLIENT_VERSION = "0.3.4"
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
SHELL_FENCE = re.compile(
    r"```(?:bash|sh|shell|zsh|powershell|pwsh)[^\n]*\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
SHELL_PLACEHOLDER = re.compile(r"<[^>\n]+>")


def _read(path: Path, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read {path.relative_to(ROOT)}: {exc}")
        return ""


def _relative_link_target(raw: str) -> str | None:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        # Markdown permits a title after a whitespace-delimited target.
        value = value.split(maxsplit=1)[0]
    lower = value.lower()
    if (
        not value
        or value.startswith("#")
        or lower.startswith(("http://", "https://", "mailto:", "data:"))
    ):
        return None
    return unquote(value.split("#", 1)[0].split("?", 1)[0])


def check_repository(root: Path = ROOT) -> list[str]:
    global ROOT
    original_root = ROOT
    ROOT = root.resolve()
    errors: list[str] = []
    try:
        for relative in CORE_DOCUMENTS:
            if not (ROOT / relative).is_file():
                errors.append(f"missing core document: {relative}")

        deployment_root = ROOT / "deploy" / "el9"
        if deployment_root.is_dir():
            for source in sorted(deployment_root.iterdir()):
                if source.is_file() and RETIRED_PUBLIC_IP in _read(source, errors):
                    errors.append(
                        f"retired public IP remains in production deployment asset: "
                        f"{source.relative_to(ROOT)}"
                    )

        sbom_path = ROOT / "sbom" / "server-runtime.spdx.json"
        if sbom_path.is_file():
            try:
                sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"cannot read SPDX SBOM: {exc}")
            else:
                if sbom.get("spdxVersion") != "SPDX-2.3":
                    errors.append("SPDX SBOM must use SPDX-2.3")
                if sbom.get("dataLicense") != "CC0-1.0":
                    errors.append("SPDX SBOM dataLicense must be CC0-1.0")
                packages = sbom.get("packages")
                if not isinstance(packages, list) or len(packages) < 2:
                    errors.append("SPDX SBOM must inventory application dependencies")
                relationships = sbom.get("relationships")
                if not isinstance(relationships, list) or not relationships:
                    errors.append("SPDX SBOM must contain dependency relationships")

        markdown = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
        for source in markdown:
            if not source.is_file():
                continue
            text = _read(source, errors)
            for block in SHELL_FENCE.finditer(text):
                placeholder = SHELL_PLACEHOLDER.search(block.group(1))
                if placeholder:
                    errors.append(
                        f"unsafe angle-bracket placeholder in shell block: "
                        f"{source.relative_to(ROOT)} -> {placeholder.group(0)}"
                    )
            for match in LINK.finditer(text):
                target = _relative_link_target(match.group(1))
                if target is None:
                    continue
                resolved = (source.parent / target).resolve()
                try:
                    resolved.relative_to(ROOT)
                except ValueError:
                    errors.append(
                        f"relative link escapes repository: "
                        f"{source.relative_to(ROOT)} -> {target}"
                    )
                    continue
                if not resolved.exists():
                    errors.append(
                        f"broken relative link: {source.relative_to(ROOT)} -> {target}"
                    )

        for relative in BASELINE_DOCUMENTS:
            path = ROOT / relative
            if not path.is_file():
                continue
            text = _read(path, errors)
            for expected in (SOURCE_DIRECTORY, PUBLIC_ENDPOINT, ADMIN_EMAIL):
                if expected not in text:
                    errors.append(f"{relative} is missing production default: {expected}")

        for relative in (
            "docs/LICENSE_SERVER_ARCHITECTURE.md",
            "docs/MANUAL_DEPLOY_EL9_IP_CERT.md",
        ):
            path = ROOT / relative
            if path.is_file():
                text = _read(path, errors)
                if f"`{CLIENT_VERSION}`" not in text:
                    errors.append(
                        f"{relative} is missing client baseline: {CLIENT_VERSION}"
                    )
                if (
                    relative == "docs/LICENSE_SERVER_ARCHITECTURE.md"
                    and f"客户端 `{RETIRED_CLIENT_VERSION}` 激活页" in text
                ):
                    errors.append(
                        f"{relative} retains retired client baseline: "
                        f"{RETIRED_CLIENT_VERSION}"
                    )
                if (
                    relative == "docs/LICENSE_SERVER_ARCHITECTURE.md"
                    and f"客户端 `{CLIENT_VERSION}` 激活页" not in text
                ):
                    errors.append(
                        f"{relative} current-delivery line drifted from client "
                        f"{CLIENT_VERSION}"
                    )

        for relative in (
            "README.md",
            "docs/ONE_CLICK_DEPLOY_AND_ADMIN.md",
            "docs/MANUAL_DEPLOY_EL9_IP_CERT.md",
            "docs/DOCUMENTATION_POLICY.md",
        ):
            path = ROOT / relative
            if path.is_file():
                text = _read(path, errors)
                for expected in (
                    "--ack-client-https-origin-ready",
                    "Client `0.3.5`",
                ):
                    if expected not in text:
                        errors.append(
                            f"{relative} is missing HTTPS client compatibility gate: "
                            f"{expected}"
                        )

        script_expectations = {
            "deploy/el9/deploy-from-data.sh": (
                f'SOURCE_DIR="{SOURCE_DIRECTORY}"',
                f'ADMIN_EMAIL="{ADMIN_EMAIL}"',
                'PUBLIC_IP="104.244.90.172"',
                'PUBLIC_PORT="9000"',
                'TLS_MODE="http"',
            ),
            "deploy/el9/one-click-deploy.sh": (
                f'SOURCE_DIR="{SOURCE_DIRECTORY}"',
                f'EMAIL="{ADMIN_EMAIL}"',
                'PUBLIC_IP="104.244.90.172"',
                'PUBLIC_PORT="9000"',
                'TLS_MODE="http"',
            ),
        }
        for relative, expected_values in script_expectations.items():
            path = ROOT / relative
            if not path.is_file():
                errors.append(f"missing deployment script: {relative}")
                continue
            text = _read(path, errors)
            for expected in expected_values:
                if expected not in text:
                    errors.append(f"{relative} default drifted: expected {expected}")

        readme = ROOT / "README.md"
        if readme.is_file() and "## 计划部署入口" in _read(readme, errors):
            errors.append("README still labels the implemented deployment as a plan")
        return errors
    finally:
        ROOT = original_root


def main() -> int:
    errors = check_repository()
    if errors:
        for error in errors:
            print(f"documentation error: {error}", file=sys.stderr)
        return 1
    print("documentation contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
