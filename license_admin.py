#!/usr/bin/env python3
"""Local administrator CLI for the WeChatMemory license database.

The tool is intentionally server-local.  It never exposes an HTTP admin API,
never stores plaintext activation codes, and records every mutation in the
SQLite audit log.  Production operators normally invoke it through the
``wkm-license-admin`` wrapper installed by the EL9 deployment script.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from register_user import (
    PLAN_IDS,
    RegistrationError,
    connect,
    ensure_schema,
    harden_file,
    initialize_database,
    iso,
    make_activation_code,
    normalize_username,
    plan_snapshot,
    register_user,
    utc_now,
)


ENTITLEMENT_STATUSES = ("active", "suspended", "expired", "revoked")
CUSTOMER_STATUSES = ("active", "disabled")
DEVICE_STATUSES = ("active", "deactivated", "blocked")


def _actor() -> str:
    value = os.environ.get("WKM_ADMIN_ACTOR", "").strip()
    return value[:100] if value else getpass.getuser()[:100]


def _audit(
    con: sqlite3.Connection,
    event_type: str,
    subject_type: str,
    subject_id: str,
    data: dict[str, Any],
    now: str,
) -> None:
    con.execute(
        "INSERT INTO audit_events(actor_type,actor_id,event_type,subject_type,subject_id,"
        "data_json,created_at) VALUES('admin_cli',?,?,?,?,?,?)",
        (
            _actor(),
            event_type,
            subject_type,
            subject_id,
            json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )


def _customer(con: sqlite3.Connection, username_value: str) -> sqlite3.Row:
    _username, normalized = normalize_username(username_value)
    row = con.execute(
        "SELECT * FROM customers WHERE username_norm=?", (normalized,)
    ).fetchone()
    if not row:
        raise RegistrationError("用户不存在")
    return row


def _entitlement(con: sqlite3.Connection, customer_id: str) -> sqlite3.Row:
    row = con.execute(
        "SELECT * FROM entitlements WHERE customer_id=? "
        "ORDER BY created_at DESC,id DESC LIMIT 1",
        (customer_id,),
    ).fetchone()
    if not row:
        raise RegistrationError("用户没有授权权益")
    return row


def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["plan_snapshot_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise RegistrationError("授权权益快照损坏") from exc
    if not isinstance(value, dict):
        raise RegistrationError("授权权益快照损坏")
    return value


def _public_device(row: sqlite3.Row) -> dict[str, Any]:
    install_id = str(row["install_id"])
    return {
        "device_id": row["id"],
        "install_id_hint": (
            install_id if len(install_id) <= 12 else f"{install_id[:6]}…{install_id[-6:]}"
        ),
        "platform": row["platform"],
        "app_version": row["app_version"],
        "status": row["status"],
        "activated_at": row["activated_at"],
        "last_seen_at": row["last_seen_at"],
        "deactivated_at": row["deactivated_at"],
    }


def _summary(con: sqlite3.Connection, customer: sqlite3.Row) -> dict[str, Any]:
    entitlement = _entitlement(con, customer["id"])
    devices = con.execute(
        "SELECT * FROM devices WHERE entitlement_id=? "
        "ORDER BY last_seen_at DESC,id",
        (entitlement["id"],),
    ).fetchall()
    credentials = con.execute(
        "SELECT token_prefix,status,created_at,last_used_at,revoked_at "
        "FROM activation_credentials WHERE entitlement_id=? "
        "ORDER BY created_at DESC",
        (entitlement["id"],),
    ).fetchall()
    return {
        "customer_id": customer["id"],
        "username": customer["username"],
        "display_name": customer["display_name"],
        "customer_status": customer["status"],
        "entitlement_id": entitlement["id"],
        "entitlement_status": entitlement["status"],
        "plan_id": entitlement["plan_id"],
        "starts_at": entitlement["starts_at"],
        "expires_at": entitlement["expires_at"],
        "upgrade_until": entitlement["upgrade_until"],
        "plan": _snapshot(entitlement),
        "active_devices": sum(row["status"] == "active" for row in devices),
        "devices": [_public_device(row) for row in devices],
        "activation_credentials": [dict(row) for row in credentials],
    }


def list_users(db_path: Path) -> dict[str, Any]:
    con = connect(db_path)
    try:
        ensure_schema(con)
        rows = con.execute(
            """
            SELECT c.id,c.username,c.display_name,c.status,
                   e.plan_id,e.status AS entitlement_status,e.expires_at,
                   e.plan_snapshot_json,
                   SUM(CASE WHEN d.status='active' THEN 1 ELSE 0 END) AS active_devices
            FROM customers c
            JOIN entitlements e ON e.id=(
                SELECT e2.id FROM entitlements e2 WHERE e2.customer_id=c.id
                ORDER BY e2.created_at DESC,e2.id DESC LIMIT 1
            )
            LEFT JOIN devices d ON d.entitlement_id=e.id
            GROUP BY c.id,e.id ORDER BY c.created_at,c.username_norm
            """
        ).fetchall()
        users = []
        for row in rows:
            item = dict(row)
            item["device_limit"] = int(
                json.loads(item.pop("plan_snapshot_json"))["device_limit"]
            )
            users.append(item)
        return {"users": users, "count": len(users)}
    finally:
        con.close()


def show_user(db_path: Path, username: str) -> dict[str, Any]:
    con = connect(db_path)
    try:
        ensure_schema(con)
        return _summary(con, _customer(con, username))
    finally:
        con.close()


def _deactivate_excess_devices(
    con: sqlite3.Connection,
    entitlement_id: str,
    device_limit: int,
    allow: bool,
    now: str,
) -> list[str]:
    rows = con.execute(
        "SELECT id FROM devices WHERE entitlement_id=? AND status='active' "
        "ORDER BY last_seen_at DESC,activated_at DESC,id",
        (entitlement_id,),
    ).fetchall()
    if len(rows) <= device_limit:
        return []
    if not allow:
        raise RegistrationError(
            f"当前有 {len(rows)} 台活跃设备，低于该数量前请先解绑设备；"
            "或明确使用 --deactivate-excess"
        )
    removed = [str(row["id"]) for row in rows[device_limit:]]
    placeholders = ",".join("?" for _ in removed)
    con.execute(
        f"UPDATE devices SET status='deactivated',deactivated_at=? "
        f"WHERE id IN ({placeholders})",
        (now, *removed),
    )
    con.execute(
        f"UPDATE refresh_sessions SET status='revoked',revoked_at=? "
        f"WHERE device_id IN ({placeholders}) AND status='active'",
        (now, *removed),
    )
    return removed


def set_plan(
    db_path: Path,
    username: str,
    plan_id: str,
    days: int | None,
    device_limit: int | None = None,
    offline_days: int | None = None,
    deactivate_excess: bool = False,
) -> dict[str, Any]:
    if days is not None and days <= 0:
        raise RegistrationError("--days 必须是正整数")
    if device_limit is not None and device_limit <= 0:
        raise RegistrationError("--device-limit 必须是正整数")
    if offline_days is not None and offline_days < 0:
        raise RegistrationError("--offline-days 不能小于 0")
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        customer = _customer(con, username)
        entitlement = _entitlement(con, customer["id"])
        plan = con.execute(
            "SELECT * FROM plans WHERE id=? AND active=1", (plan_id,)
        ).fetchone()
        if not plan:
            raise RegistrationError("套餐不存在或已停用")
        if plan["billing_kind"] == "subscription" and days is None:
            raise RegistrationError("订阅套餐必须提供 --days")
        if plan["billing_kind"] != "subscription" and days is not None:
            raise RegistrationError("只有订阅套餐可以提供 --days")
        now_dt = utc_now()
        now = iso(now_dt)
        snapshot = plan_snapshot(plan)
        if device_limit is not None:
            snapshot["device_limit"] = device_limit
        if offline_days is not None:
            snapshot["offline_grace_days"] = offline_days
        expires_at = iso(now_dt + timedelta(days=days)) if days else None
        upgrade_until = (
            iso(now_dt + timedelta(days=int(plan["upgrade_days"])))
            if plan["upgrade_days"] is not None
            else None
        )
        deactivated = _deactivate_excess_devices(
            con,
            entitlement["id"],
            int(snapshot["device_limit"]),
            deactivate_excess,
            now,
        )
        con.execute(
            "UPDATE entitlements SET plan_id=?,status='active',starts_at=?,expires_at=?,"
            "upgrade_until=?,plan_snapshot_json=?,updated_at=? WHERE id=?",
            (
                plan_id,
                now,
                expires_at,
                upgrade_until,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                now,
                entitlement["id"],
            ),
        )
        _audit(
            con,
            "entitlement.plan_changed",
            "entitlement",
            entitlement["id"],
            {
                "from_plan": entitlement["plan_id"],
                "to_plan": plan_id,
                "device_limit": snapshot["device_limit"],
                "offline_grace_days": snapshot["offline_grace_days"],
                "deactivated_devices": len(deactivated),
            },
            now,
        )
        con.commit()
        harden_file(db_path)
        return show_user(db_path, customer["username"])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def set_limits(
    db_path: Path,
    username: str,
    device_limit: int | None,
    offline_days: int | None,
    deactivate_excess: bool = False,
) -> dict[str, Any]:
    if device_limit is None and offline_days is None:
        raise RegistrationError("至少提供 --device-limit 或 --offline-days")
    if device_limit is not None and device_limit <= 0:
        raise RegistrationError("--device-limit 必须是正整数")
    if offline_days is not None and offline_days < 0:
        raise RegistrationError("--offline-days 不能小于 0")
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        customer = _customer(con, username)
        entitlement = _entitlement(con, customer["id"])
        snapshot = _snapshot(entitlement)
        old_device_limit = int(snapshot["device_limit"])
        old_offline_days = int(snapshot["offline_grace_days"])
        if device_limit is not None:
            snapshot["device_limit"] = device_limit
        if offline_days is not None:
            snapshot["offline_grace_days"] = offline_days
        now = iso(utc_now())
        deactivated = _deactivate_excess_devices(
            con,
            entitlement["id"],
            int(snapshot["device_limit"]),
            deactivate_excess,
            now,
        )
        con.execute(
            "UPDATE entitlements SET plan_snapshot_json=?,updated_at=? WHERE id=?",
            (
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                now,
                entitlement["id"],
            ),
        )
        _audit(
            con,
            "entitlement.limits_changed",
            "entitlement",
            entitlement["id"],
            {
                "device_limit": [old_device_limit, snapshot["device_limit"]],
                "offline_grace_days": [old_offline_days, snapshot["offline_grace_days"]],
                "deactivated_devices": len(deactivated),
            },
            now,
        )
        con.commit()
        harden_file(db_path)
        return show_user(db_path, customer["username"])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def extend_subscription(db_path: Path, username: str, days: int) -> dict[str, Any]:
    if days <= 0:
        raise RegistrationError("--days 必须是正整数")
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        customer = _customer(con, username)
        entitlement = _entitlement(con, customer["id"])
        snapshot = _snapshot(entitlement)
        if snapshot.get("billing_kind") != "subscription":
            raise RegistrationError("只有订阅权益可以续期")
        now_dt = utc_now()
        old_expiry = entitlement["expires_at"]
        if old_expiry:
            parsed = datetime.fromisoformat(str(old_expiry).replace("Z", "+00:00"))
            base = max(now_dt, parsed.astimezone(timezone.utc))
        else:
            base = now_dt
        expires_at = iso(base + timedelta(days=days))
        now = iso(now_dt)
        con.execute(
            "UPDATE entitlements SET status='active',expires_at=?,updated_at=? WHERE id=?",
            (expires_at, now, entitlement["id"]),
        )
        _audit(
            con,
            "entitlement.extended",
            "entitlement",
            entitlement["id"],
            {"days": days, "old_expires_at": old_expiry, "expires_at": expires_at},
            now,
        )
        con.commit()
        harden_file(db_path)
        return show_user(db_path, customer["username"])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def set_entitlement_status(
    db_path: Path, username: str, status: str
) -> dict[str, Any]:
    if status not in ENTITLEMENT_STATUSES:
        raise RegistrationError("不支持的授权状态")
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        customer = _customer(con, username)
        entitlement = _entitlement(con, customer["id"])
        if status == "active" and entitlement["expires_at"]:
            expiry = datetime.fromisoformat(
                str(entitlement["expires_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if expiry <= utc_now():
                raise RegistrationError("授权已到期，请先使用 extend 续期")
        now = iso(utc_now())
        con.execute(
            "UPDATE entitlements SET status=?,updated_at=? WHERE id=?",
            (status, now, entitlement["id"]),
        )
        _audit(
            con,
            "entitlement.status_changed",
            "entitlement",
            entitlement["id"],
            {"from": entitlement["status"], "to": status},
            now,
        )
        con.commit()
        harden_file(db_path)
        return show_user(db_path, customer["username"])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def set_customer_status(
    db_path: Path, username: str, status: str
) -> dict[str, Any]:
    if status not in CUSTOMER_STATUSES:
        raise RegistrationError("不支持的用户状态")
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        customer = _customer(con, username)
        now = iso(utc_now())
        con.execute(
            "UPDATE customers SET status=?,updated_at=? WHERE id=?",
            (status, now, customer["id"]),
        )
        _audit(
            con,
            "customer.status_changed",
            "customer",
            customer["id"],
            {"from": customer["status"], "to": status},
            now,
        )
        con.commit()
        harden_file(db_path)
        return show_user(db_path, customer["username"])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def reset_activation_code(
    db_path: Path, username: str, keep_sessions: bool = False
) -> dict[str, Any]:
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        customer = _customer(con, username)
        entitlement = _entitlement(con, customer["id"])
        now = iso(utc_now())
        prefix, activation_code, digest = make_activation_code()
        con.execute(
            "UPDATE activation_credentials SET status='revoked',revoked_at=? "
            "WHERE entitlement_id=? AND status='active'",
            (now, entitlement["id"]),
        )
        con.execute(
            "INSERT INTO activation_credentials(id,customer_id,entitlement_id,token_prefix,"
            "secret_hash,status,created_at) VALUES(?,?,?,?,?,'active',?)",
            (str(uuid.uuid4()), customer["id"], entitlement["id"], prefix, digest, now),
        )
        if not keep_sessions:
            con.execute(
                "UPDATE refresh_sessions SET status='revoked',revoked_at=? "
                "WHERE status='active' AND device_id IN "
                "(SELECT id FROM devices WHERE entitlement_id=?)",
                (now, entitlement["id"]),
            )
        _audit(
            con,
            "activation_code.reset",
            "entitlement",
            entitlement["id"],
            {"sessions_revoked": not keep_sessions},
            now,
        )
        con.commit()
        harden_file(db_path)
        return {
            "username": customer["username"],
            "entitlement_id": entitlement["id"],
            "activation_code": activation_code,
            "sessions_revoked": not keep_sessions,
            "activation_code_notice": "只显示一次；数据库不保存明文",
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def list_devices(db_path: Path, username: str) -> dict[str, Any]:
    summary = show_user(db_path, username)
    return {
        "username": summary["username"],
        "device_limit": summary["plan"]["device_limit"],
        "active_devices": summary["active_devices"],
        "devices": summary["devices"],
    }


def set_device_status(
    db_path: Path, username: str, device_id: str, status: str
) -> dict[str, Any]:
    if status not in DEVICE_STATUSES:
        raise RegistrationError("不支持的设备状态")
    con = connect(db_path)
    try:
        ensure_schema(con)
        con.execute("BEGIN IMMEDIATE")
        customer = _customer(con, username)
        entitlement = _entitlement(con, customer["id"])
        device = con.execute(
            "SELECT * FROM devices WHERE id=? AND entitlement_id=?",
            (device_id, entitlement["id"]),
        ).fetchone()
        if not device:
            raise RegistrationError("设备不存在或不属于该用户")
        if status == "active" and device["status"] != "active":
            active_count = con.execute(
                "SELECT COUNT(*) FROM devices WHERE entitlement_id=? AND status='active'",
                (entitlement["id"],),
            ).fetchone()[0]
            if active_count >= int(_snapshot(entitlement)["device_limit"]):
                raise RegistrationError("设备数量已达到授权上限")
        now = iso(utc_now())
        con.execute(
            "UPDATE devices SET status=?,deactivated_at=? WHERE id=?",
            (status, None if status == "active" else now, device_id),
        )
        if status != "active":
            con.execute(
                "UPDATE refresh_sessions SET status='revoked',revoked_at=? "
                "WHERE device_id=? AND status='active'",
                (now, device_id),
            )
        _audit(
            con,
            "device.status_changed",
            "device",
            device_id,
            {"from": device["status"], "to": status},
            now,
        )
        con.commit()
        harden_file(db_path)
        return list_devices(db_path, customer["username"])
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="微信记忆库授权服务本机管理员工具")
    root.add_argument("--db", type=Path, required=True, help="SQLite3 授权数据库")
    root.add_argument("--json", action="store_true", help="输出 JSON")
    commands = root.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="初始化或校验数据库 schema")

    add = commands.add_parser("add", help="新增用户并生成一次性激活码")
    add.add_argument("--username", required=True)
    add.add_argument("--display-name", default="")
    add.add_argument("--plan", required=True, choices=PLAN_IDS)
    add.add_argument("--days", type=int)

    commands.add_parser("list", help="列出用户和当前权益")
    show = commands.add_parser("show", help="查看一个用户的完整授权状态")
    show.add_argument("--username", required=True)

    plan = commands.add_parser("set-plan", help="更换套餐并重建权益快照")
    plan.add_argument("--username", required=True)
    plan.add_argument("--plan", required=True, choices=PLAN_IDS)
    plan.add_argument("--days", type=int, help="订阅套餐从现在起的有效天数")
    plan.add_argument("--device-limit", type=int)
    plan.add_argument("--offline-days", type=int)
    plan.add_argument("--deactivate-excess", action="store_true")

    limits = commands.add_parser("set-limits", help="调整设备数和离线宽限")
    limits.add_argument("--username", required=True)
    limits.add_argument("--device-limit", type=int)
    limits.add_argument("--offline-days", type=int)
    limits.add_argument("--deactivate-excess", action="store_true")

    extend = commands.add_parser("extend", help="延长订阅有效期")
    extend.add_argument("--username", required=True)
    extend.add_argument("--days", required=True, type=int)

    status = commands.add_parser("set-status", help="调整权益状态")
    status.add_argument("--username", required=True)
    status.add_argument("--status", required=True, choices=ENTITLEMENT_STATUSES)

    user_status = commands.add_parser("set-user-status", help="启用或停用用户")
    user_status.add_argument("--username", required=True)
    user_status.add_argument("--status", required=True, choices=CUSTOMER_STATUSES)

    reset = commands.add_parser("reset-code", help="吊销旧激活码并生成新码")
    reset.add_argument("--username", required=True)
    reset.add_argument(
        "--keep-sessions",
        action="store_true",
        help="保留已激活设备的刷新会话；默认全部撤销",
    )

    devices = commands.add_parser("devices", help="列出用户设备")
    devices.add_argument("--username", required=True)

    device = commands.add_parser("device-status", help="解绑、封禁或恢复设备")
    device.add_argument("--username", required=True)
    device.add_argument("--device-id", required=True)
    device.add_argument("--status", required=True, choices=DEVICE_STATUSES)
    return root


def _human(result: dict[str, Any]) -> None:
    if "activation_code" in result:
        print(f"用户：{result['username']}")
        print("激活码（只显示一次）：")
        print(result["activation_code"])
        if "sessions_revoked" in result:
            print("已撤销现有刷新会话：" + ("是" if result["sessions_revoked"] else "否"))
        return
    if "users" in result:
        print("用户名\t用户状态\t授权状态\t套餐\t设备\t到期时间")
        for item in result["users"]:
            print(
                f"{item['username']}\t{item['status']}\t{item['entitlement_status']}\t"
                f"{item['plan_id']}\t{item['active_devices']}/{item['device_limit']}\t"
                f"{item['expires_at'] or '永久'}"
            )
        return
    if "database" in result:
        print(f"数据库已初始化：{result['database']}")
        return
    if "username" in result:
        print(f"用户：{result['username']}")
        if "plan_id" in result:
            print(f"套餐：{result['plan_id']}")
            print(f"用户状态：{result['customer_status']}")
            print(f"授权状态：{result['entitlement_status']}")
            print(f"到期时间：{result['expires_at'] or '永久'}")
            print(
                f"设备：{result['active_devices']}/{result['plan']['device_limit']}，"
                f"离线宽限：{result['plan']['offline_grace_days']} 天"
            )
        else:
            print(
                f"设备：{result['active_devices']}/{result['device_limit']}"
            )
        for device in result.get("devices", []):
            print(
                f"- {device['device_id']}  {device['status']}  "
                f"{device['platform']} {device['app_version']}  "
                f"最后在线 {device['last_seen_at']}"
            )
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize_database(args.db)
        elif args.command == "add":
            result = register_user(
                args.db, args.username, args.display_name, args.plan, args.days
            )
        elif args.command == "list":
            result = list_users(args.db)
        elif args.command == "show":
            result = show_user(args.db, args.username)
        elif args.command == "set-plan":
            result = set_plan(
                args.db,
                args.username,
                args.plan,
                args.days,
                args.device_limit,
                args.offline_days,
                args.deactivate_excess,
            )
        elif args.command == "set-limits":
            result = set_limits(
                args.db,
                args.username,
                args.device_limit,
                args.offline_days,
                args.deactivate_excess,
            )
        elif args.command == "extend":
            result = extend_subscription(args.db, args.username, args.days)
        elif args.command == "set-status":
            result = set_entitlement_status(args.db, args.username, args.status)
        elif args.command == "set-user-status":
            result = set_customer_status(args.db, args.username, args.status)
        elif args.command == "reset-code":
            result = reset_activation_code(
                args.db, args.username, args.keep_sessions
            )
        elif args.command == "devices":
            result = list_devices(args.db, args.username)
        else:
            result = set_device_status(
                args.db, args.username, args.device_id, args.status
            )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _human(result)
        return 0
    except (RegistrationError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
