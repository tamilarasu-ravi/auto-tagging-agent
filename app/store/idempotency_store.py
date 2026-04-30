from __future__ import annotations

# MVP scope: SQLite file for idempotency. Swap with Postgres + UNIQUE constraint in production.

import json
import threading
from pathlib import Path
import sqlite3

from app.models import TaggingResult


class IdempotencyStore:
    """Persists idempotency records in SQLite."""

    def __init__(self, db_path: Path) -> None:
        """Initializes SQLite-backed idempotency storage.

        Args:
            db_path: SQLite file path for shared runtime state.
        """
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        """Creates idempotency table schema."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, idempotency_key)
                )
                """
            )

    def get(self, tenant_id: str, idempotency_key: str) -> tuple[str, TaggingResult] | None:
        """Reads a previously persisted idempotency result if present.

        Args:
            tenant_id: Tenant identifier.
            idempotency_key: Key to look up.

        Returns:
            Fingerprint and cached result when present, otherwise None.
        """
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    """
                    SELECT fingerprint, result_json
                    FROM idempotency
                    WHERE tenant_id = ? AND idempotency_key = ?
                    """,
                    (tenant_id, idempotency_key),
                ).fetchone()
            if row is None:
                return None
            return row[0], TaggingResult(**json.loads(row[1]))

    def put(
        self,
        tenant_id: str,
        idempotency_key: str,
        fingerprint: str,
        result: TaggingResult,
    ) -> None:
        """Stores and persists an idempotency record for a tenant.

        Args:
            tenant_id: Tenant identifier.
            idempotency_key: Key to save.
            fingerprint: Stable request fingerprint used for conflict detection.
            result: Final tagging result to return for retries.
        """
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO idempotency (tenant_id, idempotency_key, fingerprint, result_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        idempotency_key,
                        fingerprint,
                        json.dumps(result.model_dump(mode="json"), ensure_ascii=True),
                    ),
                )
