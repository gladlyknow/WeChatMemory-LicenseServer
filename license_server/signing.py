from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class TicketSigner:
    def __init__(self, private_key: Ed25519PrivateKey, key_id: str):
        if not key_id or len(key_id) > 128:
            raise ValueError("SIGNING_KEY_ID is invalid")
        self.private_key = private_key
        self.key_id = key_id

    @classmethod
    def load(cls, path: Path, key_id: str) -> "TicketSigner":
        raw = path.expanduser().resolve().read_bytes()
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("signing key must be Ed25519")
        return cls(key, key_id)

    @classmethod
    def generate(cls, path: Path, key_id: str) -> "TicketSigner":
        target = path.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"signing key already exists: {target}")
        key = Ed25519PrivateKey.generate()
        target.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        target.chmod(0o600)
        return cls(key, key_id)

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
