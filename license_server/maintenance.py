from __future__ import annotations

import sqlite3
from dataclasses import dataclass


DEFAULT_REFRESH_SESSION_RETENTION_DAYS = 90
DEFAULT_REFRESH_SESSION_BATCH_SIZE = 500
DEFAULT_REFRESH_SESSION_MAX_BATCHES = 20
MAX_REFRESH_SESSION_BATCH_SIZE = 500
MAX_REFRESH_SESSION_MAX_BATCHES = 100


class MaintenanceError(ValueError):
    pass


@dataclass(frozen=True)
class PruneBatchResult:
    rotated: int
    revoked: int

    @property
    def deleted(self) -> int:
        return self.rotated + self.revoked


def validate_prune_limits(batch_size: int, max_batches: int) -> None:
    if not 1 <= batch_size <= MAX_REFRESH_SESSION_BATCH_SIZE:
        raise MaintenanceError(
            f"batch_size must be between 1 and {MAX_REFRESH_SESSION_BATCH_SIZE}"
        )
    if not 1 <= max_batches <= MAX_REFRESH_SESSION_MAX_BATCHES:
        raise MaintenanceError(
            f"max_batches must be between 1 and {MAX_REFRESH_SESSION_MAX_BATCHES}"
        )


def prune_refresh_sessions_batch(
    connection: sqlite3.Connection,
    *,
    cutoff: str,
    batch_size: int,
) -> PruneBatchResult:
    """Delete one fixed-size batch of old terminal refresh sessions.

    The caller must hold a write transaction.  The candidate query is backed
    by status-specific partial indexes.  Active, expired, recent and malformed
    terminal rows are deliberately outside the deletion predicate.
    """

    validate_prune_limits(batch_size, 1)
    candidates = connection.execute(
        """
        SELECT id,status FROM (
            SELECT id,status,rotated_at AS terminal_at
            FROM refresh_sessions
            WHERE status='rotated' AND rotated_at IS NOT NULL AND rotated_at < ?
            UNION ALL
            SELECT id,status,revoked_at AS terminal_at
            FROM refresh_sessions
            WHERE status='revoked' AND revoked_at IS NOT NULL AND revoked_at < ?
        )
        ORDER BY terminal_at,id
        LIMIT ?
        """,
        (cutoff, cutoff, batch_size),
    ).fetchall()
    if not candidates:
        return PruneBatchResult(rotated=0, revoked=0)

    identifiers = [str(row[0]) for row in candidates]
    counts = {
        "rotated": sum(str(row[1]) == "rotated" for row in candidates),
        "revoked": sum(str(row[1]) == "revoked" for row in candidates),
    }
    placeholders = ",".join("?" for _ in identifiers)
    cursor = connection.execute(
        f"DELETE FROM refresh_sessions WHERE id IN ({placeholders}) AND ("
        "(status='rotated' AND rotated_at IS NOT NULL AND rotated_at < ?) OR "
        "(status='revoked' AND revoked_at IS NOT NULL AND revoked_at < ?))",
        (*identifiers, cutoff, cutoff),
    )
    if cursor.rowcount != len(identifiers):
        raise MaintenanceError(
            "refresh session prune candidate set changed during the write transaction"
        )
    return PruneBatchResult(rotated=counts["rotated"], revoked=counts["revoked"])


def has_prunable_refresh_sessions(
    connection: sqlite3.Connection,
    *,
    cutoff: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM (
            SELECT rotated_at AS terminal_at
            FROM refresh_sessions
            WHERE status='rotated' AND rotated_at IS NOT NULL AND rotated_at < ?
            UNION ALL
            SELECT revoked_at AS terminal_at
            FROM refresh_sessions
            WHERE status='revoked' AND revoked_at IS NOT NULL AND revoked_at < ?
        )
        LIMIT 1
        """,
        (cutoff, cutoff),
    ).fetchone()
    return row is not None
