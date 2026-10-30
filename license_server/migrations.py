from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping


MIGRATION_ROOT = Path(__file__).resolve().parents[1] / "migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    def checksum(self) -> str:
        try:
            payload = self.path.read_bytes()
        except OSError as exc:
            raise MigrationError(
                f"无法读取 migration 文件：{self.path.name}"
            ) from exc
        return hashlib.sha256(payload).hexdigest()


MIGRATIONS = (
    Migration(1, "initial-license-schema", MIGRATION_ROOT / "001_initial.sql"),
    Migration(2, "refresh-token-sessions", MIGRATION_ROOT / "002_refresh_sessions.sql"),
    Migration(
        3,
        "refresh-session-retention-indexes",
        MIGRATION_ROOT / "003_refresh_session_retention.sql",
    ),
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SchemaContract:
    """Minimum semantic SQLite shape required by one schema version."""

    tables: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    primary_keys: tuple[tuple[str, tuple[str, ...]], ...]
    not_null: tuple[tuple[str, tuple[str, ...]], ...]
    indexes: tuple[tuple[str, bool, tuple[str, ...]], ...]
    foreign_keys: tuple[tuple[str, str, str, str, str], ...]
    partial_indexes: tuple[
        tuple[str, str, tuple[str, ...], str], ...
    ] = ()


_V1_TABLES = (
    ("schema_meta", (("key", "TEXT"), ("value", "TEXT"))),
    (
        "customers",
        (
            ("id", "TEXT"), ("username", "TEXT"), ("username_norm", "TEXT"),
            ("display_name", "TEXT"), ("status", "TEXT"), ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
        ),
    ),
    (
        "plans",
        (
            ("id", "TEXT"), ("name", "TEXT"), ("billing_kind", "TEXT"),
            ("device_limit", "INTEGER"), ("offline_grace_days", "INTEGER"),
            ("upgrade_days", "INTEGER"), ("features_json", "TEXT"),
            ("active", "INTEGER"), ("created_at", "TEXT"), ("updated_at", "TEXT"),
        ),
    ),
    (
        "entitlements",
        (
            ("id", "TEXT"), ("customer_id", "TEXT"), ("plan_id", "TEXT"),
            ("status", "TEXT"), ("starts_at", "TEXT"), ("expires_at", "TEXT"),
            ("upgrade_until", "TEXT"), ("plan_snapshot_json", "TEXT"),
            ("created_at", "TEXT"), ("updated_at", "TEXT"),
        ),
    ),
    (
        "activation_credentials",
        (
            ("id", "TEXT"), ("customer_id", "TEXT"), ("entitlement_id", "TEXT"),
            ("token_prefix", "TEXT"), ("secret_hash", "TEXT"), ("status", "TEXT"),
            ("created_at", "TEXT"), ("last_used_at", "TEXT"), ("revoked_at", "TEXT"),
        ),
    ),
    (
        "devices",
        (
            ("id", "TEXT"), ("entitlement_id", "TEXT"), ("install_id", "TEXT"),
            ("device_hash", "TEXT"), ("platform", "TEXT"), ("app_version", "TEXT"),
            ("status", "TEXT"), ("activated_at", "TEXT"), ("last_seen_at", "TEXT"),
            ("deactivated_at", "TEXT"),
        ),
    ),
    (
        "audit_events",
        (
            ("id", "INTEGER"), ("actor_type", "TEXT"), ("actor_id", "TEXT"),
            ("event_type", "TEXT"), ("subject_type", "TEXT"), ("subject_id", "TEXT"),
            ("data_json", "TEXT"), ("created_at", "TEXT"),
        ),
    ),
)
_V1_PRIMARY_KEYS = tuple(
    (table, ("key",) if table == "schema_meta" else ("id",))
    for table, _columns in _V1_TABLES
)
_V1_NOT_NULL = (
    ("schema_meta", ("value",)),
    (
        "customers",
        ("username", "username_norm", "display_name", "status", "created_at", "updated_at"),
    ),
    (
        "plans",
        (
            "name", "billing_kind", "device_limit", "offline_grace_days",
            "features_json", "active", "created_at", "updated_at",
        ),
    ),
    (
        "entitlements",
        (
            "customer_id", "plan_id", "status", "starts_at",
            "plan_snapshot_json", "created_at", "updated_at",
        ),
    ),
    (
        "activation_credentials",
        (
            "customer_id", "entitlement_id", "token_prefix", "secret_hash",
            "status", "created_at",
        ),
    ),
    (
        "devices",
        (
            "entitlement_id", "install_id", "device_hash", "platform",
            "app_version", "status", "activated_at", "last_seen_at",
        ),
    ),
    (
        "audit_events",
        (
            "actor_type", "actor_id", "event_type", "subject_type",
            "subject_id", "data_json", "created_at",
        ),
    ),
)
_V1_INDEXES = (
    ("customers", True, ("username_norm",)),
    ("activation_credentials", True, ("token_prefix",)),
    ("activation_credentials", True, ("secret_hash",)),
    ("devices", True, ("entitlement_id", "install_id")),
    ("entitlements", False, ("customer_id", "status")),
    ("devices", False, ("entitlement_id", "status")),
    ("audit_events", False, ("subject_type", "subject_id", "created_at")),
)
_V1_FOREIGN_KEYS = (
    ("entitlements", "customer_id", "customers", "id", "RESTRICT"),
    ("entitlements", "plan_id", "plans", "id", "RESTRICT"),
    ("activation_credentials", "customer_id", "customers", "id", "RESTRICT"),
    ("activation_credentials", "entitlement_id", "entitlements", "id", "RESTRICT"),
    ("devices", "entitlement_id", "entitlements", "id", "RESTRICT"),
)
_REFRESH_TABLE = (
    "refresh_sessions",
    (
        ("id", "TEXT"), ("device_id", "TEXT"), ("token_hash", "TEXT"),
        ("status", "TEXT"), ("created_at", "TEXT"), ("expires_at", "TEXT"),
        ("last_used_at", "TEXT"), ("rotated_at", "TEXT"), ("revoked_at", "TEXT"),
        ("client_version", "TEXT"), ("platform", "TEXT"),
    ),
)

SCHEMA_CONTRACTS: Mapping[int, SchemaContract] = {
    1: SchemaContract(
        tables=_V1_TABLES,
        primary_keys=_V1_PRIMARY_KEYS,
        not_null=_V1_NOT_NULL,
        indexes=_V1_INDEXES,
        foreign_keys=_V1_FOREIGN_KEYS,
    ),
    2: SchemaContract(
        tables=_V1_TABLES + (_REFRESH_TABLE,),
        primary_keys=_V1_PRIMARY_KEYS + (("refresh_sessions", ("id",)),),
        not_null=_V1_NOT_NULL + (
            (
                "refresh_sessions",
                (
                    "device_id", "token_hash", "status", "created_at",
                    "expires_at", "client_version", "platform",
                ),
            ),
        ),
        indexes=_V1_INDEXES + (
            ("refresh_sessions", True, ("token_hash",)),
            ("refresh_sessions", False, ("device_id", "status", "expires_at")),
        ),
        foreign_keys=_V1_FOREIGN_KEYS + (
            ("refresh_sessions", "device_id", "devices", "id", "CASCADE"),
        ),
    ),
    3: SchemaContract(
        tables=_V1_TABLES + (_REFRESH_TABLE,),
        primary_keys=_V1_PRIMARY_KEYS + (("refresh_sessions", ("id",)),),
        not_null=_V1_NOT_NULL + (
            (
                "refresh_sessions",
                (
                    "device_id", "token_hash", "status", "created_at",
                    "expires_at", "client_version", "platform",
                ),
            ),
        ),
        indexes=_V1_INDEXES + (
            ("refresh_sessions", True, ("token_hash",)),
            ("refresh_sessions", False, ("device_id", "status", "expires_at")),
        ),
        foreign_keys=_V1_FOREIGN_KEYS + (
            ("refresh_sessions", "device_id", "devices", "id", "CASCADE"),
        ),
        partial_indexes=(
            (
                "refresh_sessions",
                "idx_refresh_sessions_rotated_retention",
                ("rotated_at", "id"),
                "WHERE status='rotated' AND rotated_at IS NOT NULL",
            ),
            (
                "refresh_sessions",
                "idx_refresh_sessions_revoked_retention",
                ("revoked_at", "id"),
                "WHERE status='revoked' AND revoked_at IS NOT NULL",
            ),
        ),
    ),
}


def schema_version(connection: sqlite3.Connection) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    if table is None:
        return 0
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        raise MigrationError("schema_meta 缺少 schema_version")
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise MigrationError("schema_version 不是有效整数") from exc
    if version < 0:
        raise MigrationError("schema_version 不能为负数")
    return version


def legacy_adoption_name(version: int) -> str:
    return f"legacy-schema-v{version}-adopted"


def schema_contract_checksum(
    version: int,
    contracts: Mapping[int, SchemaContract] = SCHEMA_CONTRACTS,
) -> str:
    try:
        contract = contracts[version]
    except KeyError as exc:
        raise MigrationError(f"schema {version} 缺少最低结构合同") from exc
    contract_payload = {
        "version": version,
        "tables": contract.tables,
        "primary_keys": contract.primary_keys,
        "not_null": contract.not_null,
        "indexes": contract.indexes,
        "foreign_keys": contract.foreign_keys,
    }
    # Preserve the already-recorded v1/v2 contract digest.  The field only
    # enters the digest when a schema version actually requires partial indexes.
    if contract.partial_indexes:
        contract_payload["partial_indexes"] = contract.partial_indexes
    payload = json.dumps(
        contract_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def schema_contract_errors(
    connection: sqlite3.Connection,
    version: int,
    contracts: Mapping[int, SchemaContract] = SCHEMA_CONTRACTS,
) -> tuple[str, ...]:
    """Return semantic schema gaps without trusting schema_meta alone."""

    contract = contracts.get(version)
    if contract is None:
        return (f"schema {version} 没有最低结构合同",)
    errors: list[str] = []
    table_info: dict[str, list[sqlite3.Row | tuple]] = {}
    for table, required_columns in contract.tables:
        rows = connection.execute(
            f'PRAGMA table_info("{table}")'
        ).fetchall()
        table_info[table] = rows
        if not rows:
            errors.append(f"缺少表 {table}")
            continue
        observed = {str(row[1]): str(row[2]).upper() for row in rows}
        for column, declared_type in required_columns:
            if column not in observed:
                errors.append(f"{table} 缺少列 {column}")
            elif observed[column] != declared_type:
                errors.append(
                    f"{table}.{column} 类型应为 {declared_type}，实际为 {observed[column]}"
                )

    for table, expected_key in contract.primary_keys:
        rows = table_info.get(table, [])
        observed_key = tuple(
            str(row[1])
            for row in sorted(
                (row for row in rows if int(row[5]) > 0),
                key=lambda row: int(row[5]),
            )
        )
        if rows and observed_key != expected_key:
            errors.append(
                f"{table} 主键应为 {expected_key}，实际为 {observed_key}"
            )

    for table, required_columns in contract.not_null:
        rows = table_info.get(table, [])
        observed = {str(row[1]) for row in rows if int(row[3]) == 1}
        missing = sorted(set(required_columns) - observed)
        if rows and missing:
            errors.append(f"{table} 缺少 NOT NULL 约束：{', '.join(missing)}")

    indexes_by_table: dict[str, set[tuple[bool, tuple[str, ...]]]] = {}
    for table, _required_columns in contract.tables:
        observed_indexes: set[tuple[bool, tuple[str, ...]]] = set()
        for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
            # A partial index only covers a subset of rows and therefore cannot
            # satisfy a minimum-contract requirement for the whole table.
            if len(row) > 4 and bool(row[4]):
                continue
            name = str(row[1]).replace('"', '""')
            columns = tuple(
                str(item[2])
                for item in connection.execute(
                    f'PRAGMA index_info("{name}")'
                ).fetchall()
            )
            observed_indexes.add((bool(row[2]), columns))
        indexes_by_table[table] = observed_indexes
    for table, unique, columns in contract.indexes:
        if (unique, columns) not in indexes_by_table.get(table, set()):
            kind = "UNIQUE " if unique else ""
            errors.append(f"{table} 缺少 {kind}索引 {columns}")

    for table, expected_name, expected_columns, expected_predicate in contract.partial_indexes:
        matching = None
        for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
            if str(row[1]) == expected_name:
                matching = row
                break
        if matching is None or len(matching) <= 4 or not bool(matching[4]):
            errors.append(f"{table} 缺少部分索引 {expected_name}")
            continue
        escaped_name = expected_name.replace('"', '""')
        columns = tuple(
            str(item[2])
            for item in connection.execute(
                f'PRAGMA index_info("{escaped_name}")'
            ).fetchall()
        )
        if columns != expected_columns:
            errors.append(
                f"{table} 部分索引 {expected_name} 应为 {expected_columns}，实际为 {columns}"
            )
        sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (expected_name,),
        ).fetchone()
        normalized_sql = " ".join(str(sql_row[0] if sql_row else "").split()).lower()
        normalized_predicate = " ".join(expected_predicate.split()).lower()
        if normalized_predicate not in normalized_sql:
            errors.append(
                f"{table} 部分索引 {expected_name} 缺少谓词 {expected_predicate}"
            )

    foreign_keys_by_table: dict[str, set[tuple[str, str, str, str]]] = {}
    for table, _required_columns in contract.tables:
        foreign_keys_by_table[table] = {
            (str(row[3]), str(row[2]), str(row[4]), str(row[6]).upper())
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
        }
    for table, source, target_table, target, on_delete in contract.foreign_keys:
        expected = (source, target_table, target, on_delete)
        if expected not in foreign_keys_by_table.get(table, set()):
            errors.append(
                f"{table}.{source} 缺少外键 {target_table}.{target} ON DELETE {on_delete}"
            )
    return tuple(errors)


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    applied_at: str,
    migrations: Iterable[Migration] = MIGRATIONS,
    contracts: Mapping[int, SchemaContract] | None = None,
) -> tuple[int, ...]:
    """Apply every pending migration in one SQLite transaction.

    A failure in any pending migration rolls the whole batch back.  Existing
    v1/v2 databases created before the migration ledger was introduced are
    adopted only after their semantic schema contract is verified.  Adoption
    records a contract digest, not a false claim that today's SQL file ran.
    """

    ordered = tuple(migrations)
    _validate_migration_set(ordered)
    latest = ordered[-1].version
    if contracts is None:
        prefix = MIGRATIONS[: len(ordered)]
        contracts = (
            {version: SCHEMA_CONTRACTS[version] for version in range(1, latest + 1)}
            if ordered == prefix
            else {}
        )
    current = schema_version(connection)
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current > latest:
        raise MigrationError(
            f"数据库 schema {current} 高于当前程序支持的 {latest}"
        )
    if (current == 0 and user_version != 0) or (
        current > 0 and user_version != current
    ):
        raise MigrationError(
            f"schema_meta={current} 与 PRAGMA user_version={user_version} 不一致"
        )
    pending = tuple(item for item in ordered if item.version > current)
    if not pending and current == latest:
        _ensure_migration_ledger(
            connection, ordered, current, applied_at, contracts
        )
        return ()

    applied: list[int] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_existing_schema(connection, current, contracts)
        ledger_existed = _migration_ledger_exists(connection)
        _create_migration_ledger(connection)
        if current > 0 and not ledger_existed:
            _record_legacy_adoption(
                connection, current, applied_at, contracts
            )
        _verify_applied_migrations(connection, ordered, current, contracts)
        for item in pending:
            if not item.path.is_file():
                raise MigrationError(f"缺少 migration 文件：{item.path.name}")
            sql = item.path.read_text(encoding="utf-8")
            for statement in _sql_statements(sql):
                connection.execute(statement)
            observed = schema_version(connection)
            observed_user = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if observed != item.version or observed_user != item.version:
                raise MigrationError(
                    f"migration {item.version} 未正确推进版本"
                )
            _raise_for_schema_contract(connection, item.version, contracts)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,sha256,applied_at) "
                "VALUES(?,?,?,?)",
                (item.version, item.name, item.checksum(), applied_at),
            )
            applied.append(item.version)
        _verify_applied_migrations(connection, ordered, latest, contracts)
        connection.commit()
    except Exception as exc:
        connection.rollback()
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError(f"数据库 migration 失败：{exc}") from exc
    return tuple(applied)


def _ensure_migration_ledger(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...],
    current: int,
    applied_at: str,
    contracts: Mapping[int, SchemaContract],
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        _validate_existing_schema(connection, current, contracts)
        ledger_existed = _migration_ledger_exists(connection)
        _create_migration_ledger(connection)
        if current > 0 and not ledger_existed:
            _record_legacy_adoption(
                connection, current, applied_at, contracts
            )
        _verify_applied_migrations(
            connection, migrations, current, contracts
        )
        connection.commit()
    except Exception as exc:
        connection.rollback()
        raise MigrationError(f"无法建立 migration 记录：{exc}") from exc


def _migration_ledger_exists(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='schema_migrations'"
    ).fetchone() is not None


def _raise_for_schema_contract(
    connection: sqlite3.Connection,
    version: int,
    contracts: Mapping[int, SchemaContract],
) -> None:
    if not contracts:
        return
    errors = schema_contract_errors(connection, version, contracts)
    if errors:
        raise MigrationError(
            f"schema {version} 不满足最低结构合同：" + "; ".join(errors)
        )


def _validate_existing_schema(
    connection: sqlite3.Connection,
    expected_version: int,
    contracts: Mapping[int, SchemaContract],
) -> None:
    if expected_version == 0:
        return
    observed = schema_version(connection)
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if observed != expected_version or user_version != expected_version:
        raise MigrationError(
            f"schema_meta={observed} 与 PRAGMA user_version={user_version} 不一致"
        )
    _raise_for_schema_contract(connection, expected_version, contracts)


def _record_legacy_adoption(
    connection: sqlite3.Connection,
    current: int,
    applied_at: str,
    contracts: Mapping[int, SchemaContract],
) -> None:
    if not contracts:
        raise MigrationError(
            "无 migration ledger 的旧库缺少可用的 schema 采用合同"
        )
    for version in range(1, current + 1):
        connection.execute(
            "INSERT INTO schema_migrations(version,name,sha256,applied_at) "
            "VALUES(?,?,?,?)",
            (
                version,
                legacy_adoption_name(version),
                schema_contract_checksum(version, contracts),
                applied_at,
            ),
        )


def _create_migration_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations("
        "version INTEGER PRIMARY KEY,name TEXT NOT NULL,"
        "sha256 TEXT NOT NULL CHECK(length(sha256)=64),applied_at TEXT NOT NULL)"
    )


def _verify_applied_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...],
    current: int,
    contracts: Mapping[int, SchemaContract],
) -> None:
    rows = connection.execute(
        "SELECT version,name,sha256 FROM schema_migrations ORDER BY version"
    ).fetchall()
    observed = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
    expected_versions = set(range(1, current + 1))
    if set(observed) != expected_versions:
        raise MigrationError(
            "migration ledger 版本集与 schema_version 不一致"
        )
    by_version = {item.version: item for item in migrations}
    for version in range(1, current + 1):
        item = by_version[version]
        native = (item.name, item.checksum())
        adopted = None
        if version in contracts:
            adopted = (
                legacy_adoption_name(version),
                schema_contract_checksum(version, contracts),
            )
        if observed[version] not in {native, adopted}:
            raise MigrationError(
                f"migration {version} 的名称或 SHA-256 与当前发行版不一致"
            )


def _validate_migration_set(migrations: tuple[Migration, ...]) -> None:
    if not migrations:
        raise MigrationError("migration 集合为空")
    versions = tuple(item.version for item in migrations)
    if versions != tuple(range(1, versions[-1] + 1)):
        raise MigrationError("migration 版本必须从 1 开始连续且有序")
    if len({item.name for item in migrations}) != len(migrations):
        raise MigrationError("migration 名称必须唯一")


def _sql_statements(sql: str) -> Iterator[str]:
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            buffer = ""
            if statement:
                yield statement
    if buffer.strip():
        raise MigrationError("migration SQL 存在未完成语句")
