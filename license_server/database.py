from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import LATEST_SCHEMA_VERSION, schema_contract_errors


MIN_SQLITE_VERSION = (3, 34, 1)


class Database:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def connect(self) -> sqlite3.Connection:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError("SQLite 3.34.1 or newer is required")
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ready(self) -> bool:
        try:
            with self.read() as connection:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                user_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                contract_errors = schema_contract_errors(
                    connection, LATEST_SCHEMA_VERSION
                )
            return bool(
                version
                and version[0] == str(LATEST_SCHEMA_VERSION)
                and user_version == LATEST_SCHEMA_VERSION
                and not contract_errors
            )
        except (OSError, sqlite3.Error):
            return False

    def deep_ready(self) -> bool:
        """Validate both the expected schema and SQLite page integrity."""
        try:
            with self.read() as connection:
                version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                user_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                contract_errors = schema_contract_errors(
                    connection, LATEST_SCHEMA_VERSION
                )
                integrity = connection.execute("PRAGMA quick_check").fetchone()
            return bool(
                version
                and version[0] == str(LATEST_SCHEMA_VERSION)
                and user_version == LATEST_SCHEMA_VERSION
                and not contract_errors
                and integrity
                and integrity[0] == "ok"
            )
        except (OSError, sqlite3.Error):
            return False
