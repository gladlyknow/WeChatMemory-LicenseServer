#!/usr/bin/env python3
"""Validate the exact candidate service environment without printing secrets."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ALLOWED_ENVIRONMENT = frozenset(
    {
        "DATABASE_PATH",
        "LICENSE_ISSUER",
        "LICENSE_AUDIENCE",
        "REFRESH_TOKEN_DAYS",
        "LICENSE_TICKET_HOURS",
        "RATE_LIMIT_PER_MINUTE",
        "RATE_LIMIT_GLOBAL_PER_MINUTE",
        "RATE_LIMIT_MAX_KEYS",
        "SIGNING_KEY_ID",
        "SIGNING_PRIVATE_KEY_PATH",
        "SIGNING_PUBLIC_KEYS_PATH",
    }
)
EXPECTED_PUBLIC_MANIFEST_PATH = Path(
    "/opt/wechat-memory-license/current/config/signing-public-keys.json"
)


def load_service_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError(f"invalid service environment line {line_number}")
        name, value = raw.split("=", 1)
        if name not in ALLOWED_ENVIRONMENT:
            raise ValueError(f"unexpected service environment key at line {line_number}")
        if name in values or not value:
            raise ValueError(f"duplicate or empty service environment key at line {line_number}")
        values[name] = value
    missing = sorted(ALLOWED_ENVIRONMENT - values.keys())
    if missing:
        raise ValueError("missing service environment keys: " + ",".join(missing))
    return values


def run_preflight(environment_path: Path, manifest_path: Path) -> None:
    values = load_service_environment(environment_path)
    configured_manifest = Path(values["SIGNING_PUBLIC_KEYS_PATH"])
    if configured_manifest != EXPECTED_PUBLIC_MANIFEST_PATH:
        raise ValueError("SIGNING_PUBLIC_KEYS_PATH does not match the service contract")

    # Before the atomic current-link switch, the final environment must name
    # the future current path while the preflight must inspect the candidate
    # manifest directly.  ExecStartPre validates that same configured path
    # again after the link switch.
    values["SIGNING_PUBLIC_KEYS_PATH"] = str(manifest_path.resolve())
    for name in ALLOWED_ENVIRONMENT:
        os.environ.pop(name, None)
    os.environ.update(values)

    # Import only after the final candidate environment is active.  This loads
    # and verifies the private key/manifest pair and validates every bounded
    # numeric setting exactly as the service process does.
    from license_server.app import create_app

    app = create_app()
    database = app.extensions["license_database"]
    if not database.ready():
        raise RuntimeError("candidate database is not ready")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_preflight(args.env, args.manifest)
    except Exception as error:  # noqa: BLE001 - deployment boundary
        # Never echo an arbitrary exception message: a future dependency could
        # include an environment value, PEM fragment or credential in it.
        if isinstance(error, PermissionError):
            category = "permission_denied"
        elif isinstance(error, FileNotFoundError):
            category = "required_file_missing"
        elif isinstance(error, ValueError):
            category = "configuration_or_signing_invalid"
        else:
            category = "runtime_preflight_failed"
        print(f"service preflight failed: {category}", file=sys.stderr)
        return 1
    print("service preflight: ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
