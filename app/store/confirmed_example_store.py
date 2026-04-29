from __future__ import annotations

import json
from pathlib import Path
import random
import sqlite3
import threading


class ConfirmedExampleStore:
    """Stores confirmed labeling examples for few-shot prompt injection."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmed_example (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    vendor_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_confirmed_example_tenant ON confirmed_example(tenant_id)"
            )

    def add_example(self, tenant_id: str, vendor_key: str, payload: dict[str, object]) -> None:
        """Persists one confirmed example."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO confirmed_example (tenant_id, vendor_key, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (tenant_id, vendor_key, json.dumps(payload, ensure_ascii=True)),
                )

    def sample_examples(
        self,
        tenant_id: str,
        *,
        exclude_vendor_key: str | None,
        tx_id: str,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Returns deterministic random few-shot examples for a tenant."""
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT vendor_key, payload_json FROM confirmed_example WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchall()

        items = [
            json.loads(row[1])
            for row in rows
            if exclude_vendor_key is None or row[0] != exclude_vendor_key
        ]
        if not items:
            return []

        rng = random.Random(hash(tx_id))
        if len(items) <= limit:
            return items
        return rng.sample(items, limit)
