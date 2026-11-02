#!/usr/bin/env python3
"""Validate the exact candidate service environment without printing secrets."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib
import importlib.metadata
import io
import os
import stat
import sys
import sysconfig
from pathlib import Path, PurePosixPath


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
WAITRESS_RUNTIME_FILES = {
    "waitress": "waitress/__init__.py",
    "waitress.compat": "waitress/compat.py",
    "waitress.server": "waitress/server.py",
    "waitress.trigger": "waitress/trigger.py",
}


class RuntimeDependencyError(RuntimeError):
    """Fail the deployment without exposing candidate filesystem details."""


def _runtime_site_roots() -> tuple[Path, ...]:
    prefix = Path(sys.prefix).resolve(strict=True)
    roots: list[Path] = []
    for name in ("purelib", "platlib"):
        raw = sysconfig.get_path(name)
        if not raw:
            continue
        root = Path(raw).resolve(strict=True)
        try:
            root.relative_to(prefix)
        except ValueError:
            raise RuntimeDependencyError(
                "runtime dependency validation failed"
            ) from None
        if root.name not in {"site-packages", "dist-packages"}:
            raise RuntimeDependencyError("runtime dependency validation failed")
        if root not in roots:
            roots.append(root)
    if not roots:
        raise RuntimeDependencyError("runtime dependency validation failed")
    return tuple(roots)


def _inside_runtime_site(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _sha256_record_value(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def _verified_waitress_record(
    distribution: importlib.metadata.Distribution,
    roots: tuple[Path, ...],
) -> dict[str, Path]:
    prefix = Path(sys.prefix).resolve(strict=True)
    raw_record = distribution.read_text("RECORD")
    if not raw_record:
        raise RuntimeDependencyError("runtime dependency validation failed")

    verified: dict[str, Path] = {}
    seen: set[str] = set()
    for row in csv.reader(io.StringIO(raw_record)):
        if len(row) != 3:
            raise RuntimeDependencyError("runtime dependency validation failed")
        relative_name, hash_spec, size_spec = row
        relative = PurePosixPath(relative_name)
        if (
            not relative_name
            or "\\" in relative_name
            or relative.is_absolute()
            or relative.as_posix() != relative_name
            or relative_name in seen
        ):
            raise RuntimeDependencyError("runtime dependency validation failed")
        seen.add(relative_name)

        unresolved = Path(distribution.locate_file(relative_name))
        try:
            unresolved.resolve(strict=False).relative_to(prefix)
        except ValueError:
            raise RuntimeDependencyError(
                "runtime dependency validation failed"
            ) from None

        if not hash_spec:
            is_record = (
                relative.name == "RECORD"
                and relative.parent.name.endswith(".dist-info")
            )
            is_bytecode = (
                relative.suffix == ".pyc" and "__pycache__" in relative.parts
            )
            if not is_record and not is_bytecode:
                raise RuntimeDependencyError("runtime dependency validation failed")
            continue
        if not hash_spec.startswith("sha256=") or hash_spec.count("=") != 1:
            raise RuntimeDependencyError("runtime dependency validation failed")
        expected_hash = hash_spec.removeprefix("sha256=")
        if not expected_hash or not size_spec.isascii() or not size_spec.isdecimal():
            raise RuntimeDependencyError("runtime dependency validation failed")

        if unresolved.is_symlink():
            raise RuntimeDependencyError("runtime dependency validation failed")
        installed = unresolved.resolve(strict=True)
        try:
            installed.relative_to(prefix)
        except ValueError:
            raise RuntimeDependencyError(
                "runtime dependency validation failed"
            ) from None
        if not stat.S_ISREG(installed.stat().st_mode):
            raise RuntimeDependencyError("runtime dependency validation failed")
        if installed.stat().st_size != int(size_spec):
            raise RuntimeDependencyError("runtime dependency validation failed")
        if _sha256_record_value(installed) != expected_hash:
            raise RuntimeDependencyError("runtime dependency validation failed")
        verified[relative_name] = installed

    for relative_name in WAITRESS_RUNTIME_FILES.values():
        installed = verified.get(relative_name)
        if installed is None or not _inside_runtime_site(installed, roots):
            raise RuntimeDependencyError("runtime dependency validation failed")
    return verified


def validate_wsgi_runtime() -> None:
    """Import and verify the exact Waitress files used by this interpreter.

    ``pip check`` and the SPDX gate validate distribution metadata, not the
    presence of every file recorded in an installed wheel.  Import the same
    dependency chain as the production entry point, then validate the critical
    source files against the wheel RECORD.  Bytecode is deliberately ignored.
    """

    try:
        roots = _runtime_site_roots()
        distribution = importlib.metadata.distribution("waitress")
        distribution_root = Path(distribution.locate_file("")).resolve(
            strict=True
        )
        if not _inside_runtime_site(distribution_root, roots):
            raise RuntimeDependencyError("runtime dependency validation failed")

        recorded = _verified_waitress_record(distribution, roots)
        imported: dict[str, object] = {}
        for module_name in WAITRESS_RUNTIME_FILES:
            imported[module_name] = importlib.import_module(module_name)

        waitress_module = imported["waitress"]
        serve = getattr(waitress_module, "serve", None)
        serve_code = getattr(serve, "__code__", None)
        if (
            not callable(serve)
            or getattr(serve, "__module__", None) != "waitress"
            or serve_code is None
            or Path(serve_code.co_filename).resolve(strict=True)
            != recorded["waitress/__init__.py"]
        ):
            raise RuntimeDependencyError("runtime dependency validation failed")

        for module_name, relative_name in WAITRESS_RUNTIME_FILES.items():
            installed = recorded[relative_name]
            module_file = getattr(imported[module_name], "__file__", None)
            if not module_file or Path(module_file).resolve(strict=True) != installed:
                raise RuntimeDependencyError("runtime dependency validation failed")
    except RuntimeDependencyError:
        raise
    except Exception:
        # Suppress the original exception chain as well as its message.  Import
        # and filesystem errors can contain absolute candidate paths.
        raise RuntimeDependencyError("runtime dependency validation failed") from None


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
    validate_wsgi_runtime()
    values = load_service_environment(environment_path)
    configured_manifest = Path(values["SIGNING_PUBLIC_KEYS_PATH"]).resolve(
        strict=True
    )
    candidate_manifest = manifest_path.resolve(strict=True)
    if configured_manifest != candidate_manifest:
        raise ValueError("SIGNING_PUBLIC_KEYS_PATH does not match the service contract")

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
