from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PUBLIC_KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_STATUSES = frozenset({"active", "retiring"})
MANIFEST_ALGORITHM = "Ed25519"
MANIFEST_ENCODING = "base64url-raw-no-padding"
MAX_MANIFEST_BYTES = 64 * 1024


class SigningKeyError(ValueError):
    pass


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_public_key(value: str) -> bytes:
    if not PUBLIC_KEY.fullmatch(value):
        raise SigningKeyError("public key is not 32-byte unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, TypeError) as exc:
        raise SigningKeyError("public key is not valid base64url") from exc
    if len(decoded) != 32:
        raise SigningKeyError("public key must decode to 32 bytes")
    return decoded


def _public_key_hashes(raw_public_key: bytes) -> tuple[str, str]:
    public_key = Ed25519PublicKey.from_public_bytes(raw_public_key)
    spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(raw_public_key).hexdigest(), hashlib.sha256(spki).hexdigest()


def load_public_key_manifest(path: Path) -> dict[str, Any]:
    source = path.expanduser().resolve()
    try:
        if source.stat().st_size > MAX_MANIFEST_BYTES:
            raise SigningKeyError("public key manifest is too large")
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SigningKeyError("public key manifest cannot be read") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema",
        "algorithm",
        "encoding",
        "active_key_id",
        "keys",
    }:
        raise SigningKeyError("public key manifest fields are invalid")
    if raw["schema"] != 1 or isinstance(raw["schema"], bool):
        raise SigningKeyError("public key manifest schema is unsupported")
    if raw["algorithm"] != MANIFEST_ALGORITHM:
        raise SigningKeyError("public key manifest algorithm is unsupported")
    if raw["encoding"] != MANIFEST_ENCODING:
        raise SigningKeyError("public key manifest encoding is unsupported")
    active_key_id = raw["active_key_id"]
    if not isinstance(active_key_id, str) or not KEY_ID.fullmatch(active_key_id):
        raise SigningKeyError("active key id is invalid")
    if not isinstance(raw["keys"], list) or not raw["keys"]:
        raise SigningKeyError("public key manifest must contain keys")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw["keys"]:
        if not isinstance(item, dict) or set(item) != {
            "key_id",
            "public_key",
            "raw_sha256",
            "spki_sha256",
            "status",
        }:
            raise SigningKeyError("public key record fields are invalid")
        key_id = item["key_id"]
        public_key = item["public_key"]
        raw_sha256 = item["raw_sha256"]
        spki_sha256 = item["spki_sha256"]
        status = item["status"]
        if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
            raise SigningKeyError("manifest key id is invalid")
        if key_id in seen:
            raise SigningKeyError("manifest contains a duplicate key id")
        if status not in MANIFEST_STATUSES:
            raise SigningKeyError("manifest key status is invalid")
        if (
            not isinstance(public_key, str)
            or not isinstance(raw_sha256, str)
            or not SHA256_HEX.fullmatch(raw_sha256)
            or not isinstance(spki_sha256, str)
            or not SHA256_HEX.fullmatch(spki_sha256)
        ):
            raise SigningKeyError("manifest public key is invalid")
        decoded = _decode_public_key(public_key)
        expected_raw, expected_spki = _public_key_hashes(decoded)
        if not secrets.compare_digest(raw_sha256, expected_raw):
            raise SigningKeyError("manifest raw public key hash does not match")
        if not secrets.compare_digest(spki_sha256, expected_spki):
            raise SigningKeyError("manifest SPKI public key hash does not match")
        seen.add(key_id)
        normalized.append(
            {
                "key_id": key_id,
                "public_key": public_key,
                "raw_sha256": raw_sha256,
                "spki_sha256": spki_sha256,
                "status": status,
            }
        )
    active = next(
        (item for item in normalized if item["key_id"] == active_key_id), None
    )
    if active is None or active["status"] != "active":
        raise SigningKeyError("active key id does not identify an active key")
    if sum(item["status"] == "active" for item in normalized) != 1:
        raise SigningKeyError("manifest must contain exactly one active key")
    return {
        "schema": 1,
        "algorithm": MANIFEST_ALGORITHM,
        "encoding": MANIFEST_ENCODING,
        "active_key_id": active_key_id,
        "keys": normalized,
    }


def public_record(signer: "TicketSigner", *, status: str = "active") -> dict[str, str]:
    if status not in MANIFEST_STATUSES:
        raise SigningKeyError("public key status is invalid")
    public_key = signer.public_key_b64()
    raw_sha256, spki_sha256 = _public_key_hashes(_decode_public_key(public_key))
    return {
        "key_id": signer.key_id,
        "public_key": public_key,
        "raw_sha256": raw_sha256,
        "spki_sha256": spki_sha256,
        "status": status,
    }


def manifest_for_signer(signer: "TicketSigner") -> dict[str, Any]:
    return {
        "schema": 1,
        "algorithm": MANIFEST_ALGORITHM,
        "encoding": MANIFEST_ENCODING,
        "active_key_id": signer.key_id,
        "keys": [public_record(signer)],
    }


def inspect_private_key(path: Path, key_id: str) -> dict[str, str]:
    return public_record(TicketSigner.load(path, key_id))


def verify_private_key(
    path: Path, key_id: str, manifest_path: Path, *, require_active: bool = True
) -> dict[str, str]:
    signer = TicketSigner.load(path, key_id)
    manifest = load_public_key_manifest(manifest_path)
    record = next(
        (item for item in manifest["keys"] if item["key_id"] == key_id), None
    )
    if record is None:
        raise SigningKeyError("key id is absent from public key manifest")
    if not secrets.compare_digest(record["public_key"], signer.public_key_b64()):
        raise SigningKeyError("private key does not match public key manifest")
    if require_active and manifest["active_key_id"] != key_id:
        raise SigningKeyError("private key is not the manifest active key")
    return dict(record)


def _write_new_file(target: Path, payload: bytes, mode: int) -> bool:
    """Publish a complete file without ever replacing an existing path."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        os.chmod(target, mode)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _replace_file(target: Path, payload: bytes, mode: int) -> None:
    """Atomically replace one regular file after its full content is durable."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _manifest_payload(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def write_new_public_key_manifest(path: Path, signer: "TicketSigner") -> None:
    payload = _manifest_payload(manifest_for_signer(signer))
    target = path.expanduser().resolve()
    if not _write_new_file(target, payload, 0o644):
        raise FileExistsError(f"public key manifest already exists: {target}")


def publish_public_key(
    path: Path, signer: "TicketSigner", *, activate: bool = False
) -> tuple[dict[str, Any], bool]:
    """Publish a signer to a manifest without replacing a key-id binding.

    The sidecar lock prevents two administrators from losing one another's
    changes.  A new key joins as ``retiring`` (verification-only in practice)
    unless activation is explicitly requested.  Existing identical entries are
    idempotent; an existing key id bound to different bytes is always rejected.
    """

    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / f".{target.name}.lock"
    try:
        lock_descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SigningKeyError(
            f"public key manifest is being updated; inspect stale lock: {lock}"
        ) from exc
    try:
        os.close(lock_descriptor)
        candidate = public_record(signer, status="active" if activate else "retiring")
        if target.exists():
            manifest = load_public_key_manifest(target)
            existing = next(
                (item for item in manifest["keys"] if item["key_id"] == signer.key_id),
                None,
            )
            if existing is not None and not secrets.compare_digest(
                existing["public_key"], candidate["public_key"]
            ):
                raise SigningKeyError(
                    "key id is already bound to a different public key; use a new key id"
                )

            changed = False
            if existing is None:
                manifest["keys"].append(candidate)
                existing = candidate
                changed = True
            if activate:
                for record in manifest["keys"]:
                    desired = "active" if record["key_id"] == signer.key_id else "retiring"
                    if record["status"] != desired:
                        record["status"] = desired
                        changed = True
                if manifest["active_key_id"] != signer.key_id:
                    manifest["active_key_id"] = signer.key_id
                    changed = True
            if not changed:
                return manifest, False
        else:
            if not activate:
                raise SigningKeyError(
                    "a new manifest needs --activate to select its first active key"
                )
            manifest = manifest_for_signer(signer)

        # Revalidate our own output before making it visible.
        staged = target.parent / f".{target.name}.{secrets.token_hex(12)}.validate"
        try:
            if not _write_new_file(staged, _manifest_payload(manifest), 0o600):
                raise SigningKeyError("failed to stage public key manifest")
            normalized = load_public_key_manifest(staged)
        finally:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
        _replace_file(target, _manifest_payload(normalized), 0o644)
        return normalized, True
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


class TicketSigner:
    def __init__(self, private_key: Ed25519PrivateKey, key_id: str):
        if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
            raise SigningKeyError("SIGNING_KEY_ID is invalid")
        self.private_key = private_key
        self.key_id = key_id

    @classmethod
    def load(cls, path: Path, key_id: str) -> "TicketSigner":
        try:
            raw = path.expanduser().resolve().read_bytes()
            key = serialization.load_pem_private_key(raw, password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise SigningKeyError("signing private key cannot be loaded") from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise SigningKeyError("signing key must be Ed25519")
        return cls(key, key_id)

    @classmethod
    def ensure(cls, path: Path, key_id: str) -> tuple["TicketSigner", bool]:
        """Load an existing key or atomically create one without overwriting it."""

        target = path.expanduser().resolve()
        if target.exists():
            return cls.load(target, key_id), False
        key = Ed25519PrivateKey.generate()
        payload = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        created = _write_new_file(target, payload, 0o600)
        return (cls(key, key_id), True) if created else (cls.load(target, key_id), False)

    @classmethod
    def generate(cls, path: Path, key_id: str) -> "TicketSigner":
        signer, created = cls.ensure(path, key_id)
        if not created:
            raise FileExistsError(f"signing key already exists: {path.expanduser().resolve()}")
        return signer

    @classmethod
    def load_verified(
        cls, path: Path, key_id: str, manifest_path: Path
    ) -> tuple["TicketSigner", dict[str, Any]]:
        manifest = load_public_key_manifest(manifest_path)
        verify_private_key(path, key_id, manifest_path, require_active=True)
        return cls.load(path, key_id), manifest

    def public_key_b64(self) -> str:
        return b64url(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.Raw,
                serialization.PublicFormat.Raw,
            )
        )

    def sign(self, claims: dict[str, Any]) -> dict[str, Any]:
        payload = dict(claims)
        payload["key_id"] = self.key_id
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        payload["signature"] = b64url(self.private_key.sign(canonical))
        return payload
