#!/usr/bin/env python3
"""Initialize the license SQLite database and register commercial users.

This administrative CLI intentionally does not verify email addresses, phone
numbers, or payments. A registered username is authenticated during product
activation with a high-entropy activation code whose digest, never plaintext,
is stored in SQLite.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from license_server.migrations import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationError,
    apply_migrations,
    schema_version,
)


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MIN_SQLITE_VERSION = (3, 34, 1)
PLAN_IDS = ("free", "permanent_personal", "pro_subscription")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

PLAN_SEEDS = {
    "free": {
        "name": "免费版",
        "billing_kind": "free",
        "device_limit": 1,
        "offline_grace_days": 30,
        "upgrade_days": None,
        "features": [
            "read_only", "manual_sync", "single_session_export", "settings_write",
        ],
    },
    "permanent_personal": {
        "name": "永久个人版",
        "billing_kind": "permanent",
        "device_limit": 2,
        "offline_grace_days": 180,
        "upgrade_days": 365,
        "features": [
            "read_only", "manual_sync", "realtime_sync", "single_account",
            "single_session_export", "classified_export", "standard_analytics",
            "settings_write",
        ],
    },
    "pro_subscription": {
        "name": "专业订阅版",
        "billing_kind": "subscription",
        "device_limit": 3,
        "offline_grace_days": 30,
        "upgrade_days": None,
        "features": [
            "read_only", "manual_sync", "realtime_sync", "multi_account",
            "single_session_export", "classified_export", "batch_export",
            "standard_analytics", "advanced_analytics", "settings_write",
        ],
    },
}


class RegistrationError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_username(value: str) -> tuple[str, str]:
    username = unicodedata.normalize("NFKC", value).strip()
    if len(username) < 3 or len(username) > 64:
        raise RegistrationError("用户名长度必须为 3～64 个字符")
    if CONTROL_RE.search(username) or any(char.isspace() for char in username):
        raise RegistrationError("用户名不能包含空白或控制字符")
    return username, username.casefold()


def connect(db_path: Path) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        minimum = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise RegistrationError(
            f"SQLite 版本过低：{sqlite3.sqlite_version}，最低需要 {minimum}"
        )
    con = sqlite3.connect(db_path, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def harden_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def seed_plans(con: sqlite3.Connection, now: str) -> None:
    for plan_id, plan in PLAN_SEEDS.items():
        features_json = json.dumps(plan["features"], ensure_ascii=False,
                                   separators=(",", ":"))
        con.execute(
            "INSERT INTO plans(id,name,billing_kind,device_limit,offline_grace_days,"
            "upgrade_days,features_json,active,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,1,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            "billing_kind=excluded.billing_kind,device_limit=excluded.device_limit,"
            "offline_grace_days=excluded.offline_grace_days,"
            "upgrade_days=excluded.upgrade_days,features_json=excluded.features_json,"
            "active=excluded.active,updated_at=excluded.updated_at",
            (plan_id, plan["name"], plan["billing_kind"], plan["device_limit"],
             plan["offline_grace_days"], plan["upgrade_days"], features_json, now, now),
        )


def initialize_database(db_path: Path) -> dict:
    missing = [item.path.name for item in MIGRATIONS if not item.path.is_file()]
    if missing:
        raise RegistrationError("缺少 migration：" + ", ".join(missing))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db_path)
    try:
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RegistrationError(f"无法启用 WAL，SQLite 返回：{mode}")
        con.execute("PRAGMA synchronous = FULL")
        migration_time = iso(utc_now())
        apply_migrations(con, applied_at=migration_time)
        with con:
            seed_plans(con, migration_time)
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RegistrationError(f"数据库完整性检查失败：{check}")
    finally:
        con.close()
    harden_file(db_path)
    return {"database": str(db_path.resolve()), "schema_version": LATEST_SCHEMA_VERSION,
            "journal_mode": "wal", "plans": list(PLAN_IDS)}


def ensure_schema(con: sqlite3.Connection) -> None:
    try:
        version = schema_version(con)
    except (sqlite3.OperationalError, MigrationError) as exc:
        raise RegistrationError("数据库尚未初始化，请先运行 init") from exc
    if version != LATEST_SCHEMA_VERSION:
        raise RegistrationError("不支持的数据库 schema 版本")


def make_activation_code() -> tuple[str, str, str]:
    prefix = secrets.token_hex(4).upper()
    secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    groups = "-".join(secret[index:index + 4] for index in range(0, len(secret), 4))
    code = f"WKM1-{prefix}-{groups}"
    digest = hashlib.sha256(("wechat-memory-license:v1:" + code).encode("utf-8")).hexdigest()
    return prefix, code, digest


def plan_snapshot(plan: sqlite3.Row) -> dict:
    return {
        "id": plan["id"],
        "name": plan["name"],
        "billing_kind": plan["billing_kind"],
        "device_limit": plan["device_limit"],
        "offline_grace_days": plan["offline_grace_days"],
        "upgrade_days": plan["upgrade_days"],
        "features": json.loads(plan["features_json"]),
    }


def register_user(db_path: Path, username_value: str, display_name: str,
                  plan_id: str, days: int | None) -> dict:
    username, username_norm = normalize_username(username_value)
    if len(display_name) > 100 or CONTROL_RE.search(display_name):
        raise RegistrationError("显示名称不合法或超过 100 个字符")
    if days is not None and days <= 0:
        raise RegistrationError("--days 必须是正整数")

    con = connect(db_path)
    try:
        ensure_schema(con)
        plan = con.execute("SELECT * FROM plans WHERE id=? AND active=1", (plan_id,)).fetchone()
        if not plan:
            raise RegistrationError("套餐不存在或已停用")
        if plan["billing_kind"] == "subscription" and days is None:
            raise RegistrationError("订阅套餐必须提供 --days")
        if plan["billing_kind"] != "subscription" and days is not None:
            raise RegistrationError("只有订阅套餐可以提供 --days")

        now_dt = utc_now()
        now = iso(now_dt)
        expires_at = iso(now_dt + timedelta(days=days)) if days else None
        upgrade_days = plan["upgrade_days"]
        upgrade_until = iso(now_dt + timedelta(days=upgrade_days)) if upgrade_days else None
        customer_id = str(uuid.uuid4())
        entitlement_id = str(uuid.uuid4())
        credential_id = str(uuid.uuid4())
        prefix, activation_code, digest = make_activation_code()
        snapshot = plan_snapshot(plan)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))

        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO customers(id,username,username_norm,display_name,status,created_at,updated_at) "
                "VALUES(?,?,?,?, 'active', ?, ?)",
                (customer_id, username, username_norm, display_name.strip(), now, now),
            )
            con.execute(
                "INSERT INTO entitlements(id,customer_id,plan_id,status,starts_at,expires_at,"
                "upgrade_until,plan_snapshot_json,created_at,updated_at) "
                "VALUES(?,?,?,'active',?,?,?,?,?,?)",
                (entitlement_id, customer_id, plan_id, now, expires_at, upgrade_until,
                 snapshot_json, now, now),
            )
            con.execute(
                "INSERT INTO activation_credentials(id,customer_id,entitlement_id,token_prefix,"
                "secret_hash,status,created_at) VALUES(?,?,?,?,?,'active',?)",
                (credential_id, customer_id, entitlement_id, prefix, digest, now),
            )
            con.execute(
                "INSERT INTO audit_events(actor_type,actor_id,event_type,subject_type,subject_id,"
                "data_json,created_at) VALUES('admin_cli','','customer.registered','customer',?,?,?)",
                (customer_id, json.dumps({"plan_id": plan_id}, separators=(",", ":")), now),
            )
            con.commit()
        except sqlite3.IntegrityError as exc:
            con.rollback()
            if "customers.username_norm" in str(exc):
                raise RegistrationError("用户名已经存在") from exc
            raise RegistrationError(f"注册失败：{exc}") from exc

        harden_file(db_path)
        return {
            "customer_id": customer_id,
            "username": username,
            "entitlement_id": entitlement_id,
            "plan": snapshot,
            "starts_at": now,
            "expires_at": expires_at,
            "upgrade_until": upgrade_until,
            "activation_code": activation_code,
            "activation_code_notice": "只显示一次；数据库不保存明文",
        }
    finally:
        con.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="微信记忆库云端授权管理员工具")
    commands = root.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="初始化 SQLite3 授权数据库")
    init.add_argument("--db", type=Path, required=True, help="SQLite3 文件路径")
    init.add_argument("--json", action="store_true", help="输出 JSON")

    add = commands.add_parser("add", help="注册用户并生成一次性显示的激活码")
    add.add_argument("--db", type=Path, required=True, help="SQLite3 文件路径")
    add.add_argument("--username", required=True, help="唯一用户名，不验证邮箱或手机号")
    add.add_argument("--display-name", default="", help="可选显示名称")
    add.add_argument("--plan", required=True, choices=PLAN_IDS)
    add.add_argument("--days", type=int, help="订阅有效天数，仅 pro_subscription 使用")
    add.add_argument("--json", action="store_true", help="输出 JSON；注意其中含一次性激活码")
    return root


def print_result(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if "activation_code" not in result:
        print(f"数据库已初始化：{result['database']}")
        print(f"schema={result['schema_version']} journal_mode={result['journal_mode']}")
        print("套餐：" + ", ".join(result["plans"]))
        return
    print("用户注册成功")
    print(f"用户名：{result['username']}")
    print(f"用户 ID：{result['customer_id']}")
    print(f"权益 ID：{result['entitlement_id']}")
    print(f"套餐：{result['plan']['name']} ({result['plan']['id']})")
    print(f"设备上限：{result['plan']['device_limit']}")
    print(f"离线期限：{result['plan']['offline_grace_days']} 天")
    print("激活码（只显示一次）：")
    print(result["activation_code"])


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_database(args.db)
        else:
            result = register_user(args.db, args.username, args.display_name,
                                   args.plan, args.days)
        print_result(result, args.json)
        return 0
    except (RegistrationError, MigrationError, sqlite3.Error, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
