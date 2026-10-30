from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import Database
from .signing import TicketSigner


HEX64 = re.compile(r"^[a-f0-9]{64}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
READ_ONLY = ("read_only",)


class LicenseError(RuntimeError):
    def __init__(self, public_code: str, *, status: int = 400):
        super().__init__(public_code)
        self.public_code = public_code
        self.status = status


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def token_hash(namespace: str, value: str) -> str:
    return hashlib.sha256(f"wechat-memory-{namespace}:v1:{value}".encode()).hexdigest()


def normalize_username(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise LicenseError("activation_failed", status=401)
    username = unicodedata.normalize("NFKC", value).strip()
    if not 3 <= len(username) <= 64 or CONTROL.search(username) or any(
        char.isspace() for char in username
    ):
        raise LicenseError("activation_failed", status=401)
    return username, username.casefold()


def require_text(payload: dict[str, Any], key: str, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LicenseError("invalid_request")
    return value.strip()


@dataclass(frozen=True)
class LicenseSettings:
    issuer: str = "wechat-memory-license"
    audience: str = "wechat-memory-client"
    refresh_days: int = 30
    ticket_hours: int = 24


class LicenseManager:
    def __init__(self, database: Database, signer: TicketSigner, settings: LicenseSettings):
        self.database = database
        self.signer = signer
        self.settings = settings

    def activate(self, payload: dict[str, Any]) -> dict[str, Any]:
        account, account_norm = normalize_username(payload.get("account"))
        activation_code = require_text(payload, "activation_code", 160).upper()
        install_id = require_text(payload, "install_id", 128)
        device_hash = require_text(payload, "device_hash", 64).casefold()
        platform = str(payload.get("platform", ""))[:80]
        app_version = str(payload.get("app_version", ""))[:40]
        if not HEX64.fullmatch(device_hash) or not 32 <= len(install_id) <= 128:
            raise LicenseError("activation_failed", status=401)
        digest = token_hash("license", activation_code)
        now = utc_now()
        now_text = iso(now)
        with self.database.write() as connection:
            row = connection.execute(
                """
                SELECT c.id AS customer_id,c.username,c.status AS customer_status,
                       e.*,a.id AS credential_id,a.status AS credential_status
                FROM customers c
                JOIN entitlements e ON e.customer_id=c.id
                JOIN activation_credentials a ON a.entitlement_id=e.id
                WHERE c.username_norm=? AND a.secret_hash=?
                ORDER BY e.created_at DESC LIMIT 1
                """,
                (account_norm, digest),
            ).fetchone()
            if not row or not self._entitlement_active(row, now):
                raise LicenseError("activation_failed", status=401)
            existing = connection.execute(
                "SELECT * FROM devices WHERE entitlement_id=? AND install_id=?",
                (row["id"], install_id),
            ).fetchone()
            if existing and existing["device_hash"] != device_hash:
                raise LicenseError("activation_failed", status=401)
            if existing:
                device_id = existing["id"]
                connection.execute(
                    "UPDATE devices SET status='active',platform=?,app_version=?,last_seen_at=?,"
                    "deactivated_at=NULL WHERE id=?",
                    (platform, app_version, now_text, device_id),
                )
            else:
                active_count = connection.execute(
                    "SELECT COUNT(*) FROM devices WHERE entitlement_id=? AND status='active'",
                    (row["id"],),
                ).fetchone()[0]
                snapshot = json.loads(row["plan_snapshot_json"])
                if active_count >= int(snapshot["device_limit"]):
                    raise LicenseError("device_limit_reached", status=409)
                device_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO devices(id,entitlement_id,install_id,device_hash,platform,"
                    "app_version,status,activated_at,last_seen_at) "
                    "VALUES(?,?,?,?,?,?,'active',?,?)",
                    (device_id, row["id"], install_id, device_hash, platform, app_version,
                     now_text, now_text),
                )
            connection.execute(
                "UPDATE refresh_sessions SET status='revoked',revoked_at=? "
                "WHERE device_id=? AND status='active'",
                (now_text, device_id),
            )
            refresh_token = self._new_refresh_session(
                connection, device_id, platform, app_version, now
            )
            connection.execute(
                "UPDATE activation_credentials SET last_used_at=? WHERE id=?",
                (now_text, row["credential_id"]),
            )
            self._audit(
                connection, "client", device_id, "device.activated", "device", device_id,
                {"plan_id": row["plan_id"]}, now_text,
            )
            response = self._response(
                row, row["username"], device_id, install_id, now
            )
        response["refresh_token"] = refresh_token
        return response

    def refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        refresh_token = require_text(payload, "refresh_token", 256)
        install_id = require_text(payload, "install_id", 128)
        device_hash = require_text(payload, "device_hash", 64).casefold()
        platform = str(payload.get("platform", ""))[:80]
        app_version = str(payload.get("app_version", ""))[:40]
        now = utc_now()
        now_text = iso(now)
        digest = token_hash("refresh", refresh_token)
        with self.database.write() as connection:
            row = self._session_row(connection, digest)
            if (
                not row
                or row["session_status"] != "active"
                or row["device_status"] != "active"
                or row["install_id"] != install_id
                or row["device_hash"] != device_hash
                or parse_time(row["session_expires_at"]) <= now
            ):
                raise LicenseError("refresh_failed", status=401)
            connection.execute(
                "UPDATE refresh_sessions SET status='rotated',last_used_at=?,rotated_at=? "
                "WHERE id=?",
                (now_text, now_text, row["session_id"]),
            )
            new_token = self._new_refresh_session(
                connection, row["device_id"], platform, app_version, now
            )
            connection.execute(
                "UPDATE devices SET platform=?,app_version=?,last_seen_at=? WHERE id=?",
                (platform, app_version, now_text, row["device_id"]),
            )
            self._audit(
                connection, "client", row["device_id"], "license.refreshed", "device",
                row["device_id"], {}, now_text,
            )
            response = self._response(
                row, row["username"], row["device_id"], row["install_id"], now
            )
        response["refresh_token"] = new_token
        return response

    def status(self, refresh_token: str) -> dict[str, Any]:
        digest = token_hash("refresh", refresh_token)
        now = utc_now()
        with self.database.read() as connection:
            row = self._session_row(connection, digest)
            if not row or row["session_status"] != "active":
                raise LicenseError("refresh_failed", status=401)
            return self._response(
                row, row["username"], row["device_id"], row["install_id"], now
            )

    def deactivate(self, payload: dict[str, Any]) -> dict[str, Any]:
        refresh_token = require_text(payload, "refresh_token", 256)
        digest = token_hash("refresh", refresh_token)
        now_text = iso(utc_now())
        with self.database.write() as connection:
            row = self._session_row(connection, digest)
            if not row or row["session_status"] != "active":
                raise LicenseError("refresh_failed", status=401)
            connection.execute(
                "UPDATE devices SET status='deactivated',deactivated_at=?,last_seen_at=? "
                "WHERE id=?",
                (now_text, now_text, row["device_id"]),
            )
            connection.execute(
                "UPDATE refresh_sessions SET status='revoked',revoked_at=? "
                "WHERE device_id=? AND status='active'",
                (now_text, row["device_id"]),
            )
            self._audit(
                connection, "client", row["device_id"], "device.deactivated", "device",
                row["device_id"], {}, now_text,
            )
        return {"deactivated": True}

    def _session_row(self, connection: sqlite3.Connection, digest: str):
        return connection.execute(
            """
            SELECT r.id AS session_id,r.status AS session_status,
                   r.expires_at AS session_expires_at,d.id AS device_id,
                   d.status AS device_status,d.install_id,d.device_hash,
                   c.username,c.status AS customer_status,e.*
            FROM refresh_sessions r
            JOIN devices d ON d.id=r.device_id
            JOIN entitlements e ON e.id=d.entitlement_id
            JOIN customers c ON c.id=e.customer_id
            WHERE r.token_hash=? LIMIT 1
            """,
            (digest,),
        ).fetchone()

    def _new_refresh_session(
        self, connection: sqlite3.Connection, device_id: str, platform: str,
        app_version: str, now: datetime,
    ) -> str:
        token = "wkmr_" + secrets.token_urlsafe(48)
        connection.execute(
            "INSERT INTO refresh_sessions(id,device_id,token_hash,status,created_at,expires_at,"
            "client_version,platform) VALUES(?,?,?,'active',?,?,?,?)",
            (str(uuid.uuid4()), device_id, token_hash("refresh", token), iso(now),
             iso(now + timedelta(days=self.settings.refresh_days)), app_version, platform),
        )
        return token

    def _entitlement_active(self, row: sqlite3.Row, now: datetime) -> bool:
        if (
            row["customer_status"] != "active"
            or row["status"] != "active"
            or ("credential_status" in row.keys() and row["credential_status"] != "active")
        ):
            return False
        return not row["expires_at"] or parse_time(row["expires_at"]) > now

    def _response(
        self, row: sqlite3.Row, account: str, device_id: str, install_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        snapshot = json.loads(row["plan_snapshot_json"])
        active = self._entitlement_active(row, now)
        status = "active" if active else "expired"
        not_before = now - timedelta(minutes=5)
        online_expiry = now + timedelta(hours=self.settings.ticket_hours)
        entitlement_expiry = parse_time(row["expires_at"]) if row["expires_at"] else None
        if entitlement_expiry is not None:
            online_expiry = min(online_expiry, max(now, entitlement_expiry))
        offline_until = (
            min(now + timedelta(days=int(snapshot["offline_grace_days"])), entitlement_expiry)
            if active and entitlement_expiry is not None
            else now + timedelta(days=int(snapshot["offline_grace_days"]))
            if active
            else now
        )
        claims = {
            "issuer": self.settings.issuer,
            "audience": self.settings.audience,
            "ticket_id": str(uuid.uuid4()),
            "account": account,
            "device_id": device_id,
            "install_id": install_id,
            "plan": row["plan_id"],
            "status": status,
            "capabilities": snapshot["features"] if active else list(READ_ONLY),
            "not_before": iso(not_before),
            "expires_at": iso(online_expiry),
            "offline_until": iso(max(online_expiry, offline_until)),
            "issued_at": iso(now),
        }
        return {
            "server_time": iso(now),
            "refresh_after": iso(now + timedelta(hours=max(1, self.settings.ticket_hours // 2))),
            "ticket": self.signer.sign(claims),
        }

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, actor_type: str, actor_id: str,
        event_type: str, subject_type: str, subject_id: str,
        data: dict[str, Any], created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(actor_type,actor_id,event_type,subject_type,subject_id,"
            "data_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (actor_type, actor_id, event_type, subject_type, subject_id,
             json.dumps(data, separators=(",", ":")), created_at),
        )
