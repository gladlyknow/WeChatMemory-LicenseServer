#!/usr/bin/env python3
"""Validate the local documentation contract without network access."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
CORE_DOCUMENTS = (
    "README.md",
    "docs/README.md",
    "docs/DOCUMENTATION_POLICY.md",
    "docs/SERVER_OPERATIONS_RUNBOOK.md",
    "docs/LICENSE_SERVER_ARCHITECTURE.md",
    "docs/ONE_CLICK_DEPLOY_AND_ADMIN.md",
    "docs/MANUAL_DEPLOY_EL9_IP_CERT.md",
    "docs/SIGNING_KEY_OPERATIONS.md",
    "docs/DEPLOYMENT_104.224.90.172.md",
)
BASELINE_DOCUMENTS = (
    "README.md",
    "docs/README.md",
    "docs/SERVER_OPERATIONS_RUNBOOK.md",
    "docs/ONE_CLICK_DEPLOY_AND_ADMIN.md",
)
SOURCE_DIRECTORY = "/data/WeChatMemory-LicenseServer"
PUBLIC_ENDPOINT = "https://104.224.90.172:9000"
ADMIN_EMAIL = "gladlygrady@tutamail.com"
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

        script_expectations = {
            "deploy/el9/deploy-from-data.sh": (
                f'SOURCE_DIR="{SOURCE_DIRECTORY}"',
                f'ADMIN_EMAIL="{ADMIN_EMAIL}"',
                'PUBLIC_IP="104.224.90.172"',
                'PUBLIC_PORT="9000"',
            ),
            "deploy/el9/one-click-deploy.sh": (
                f'SOURCE_DIR="{SOURCE_DIRECTORY}"',
                f'EMAIL="{ADMIN_EMAIL}"',
                'PUBLIC_IP="104.224.90.172"',
                'PUBLIC_PORT="9000"',
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
