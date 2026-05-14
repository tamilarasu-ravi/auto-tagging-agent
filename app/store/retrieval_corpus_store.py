"""SQLite persistence for Phase 1 retrieval corpus (human-confirmed labels, no embeddings yet)."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from app.models import RetrievalCorpusDocument, RetrievalCorpusInsert


class RetrievalCorpusStore:
    """Append-only tenant-scoped rows produced from successful review resolutions."""

    def __init__(self, db_path: Path) -> None:
        """Initializes the corpus table on the shared SQLite runtime database.

        Args:
            db_path: Path to SQLite file (same as other MVP stores).
        """
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _init_db(self) -> None:
        """Creates retrieval_corpus schema and indexes if missing."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS retrieval_corpus (
                    corpus_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    tx_id TEXT NOT NULL,
                    vendor_key TEXT NOT NULL,
                    vendor_raw TEXT,
                    amount TEXT,
                    currency TEXT,
                    transaction_date TEXT,
                    transaction_type TEXT,
                    final_coa_account_id TEXT NOT NULL,
                    suggested_coa_account_id TEXT,
                    confidence REAL,
                    resolution_action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    embedding_model TEXT,
                    embedding_version INTEGER,
                    UNIQUE (tenant_id, tx_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_retrieval_corpus_tenant "
                "ON retrieval_corpus(tenant_id)"
            )

    def insert(self, row: RetrievalCorpusInsert) -> bool:
        """Inserts one corpus row if (tenant_id, tx_id) is not already present.

        Args:
            row: Normalized payload from a review resolve path.

        Returns:
            True if a new row was inserted, False if ignored due to UNIQUE conflict.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        txn_date = row.transaction_date.isoformat() if row.transaction_date is not None else None
        txn_type = row.transaction_type if row.transaction_type is not None else None

        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO retrieval_corpus (
                        tenant_id, tx_id, vendor_key, vendor_raw, amount, currency,
                        transaction_date, transaction_type, final_coa_account_id,
                        suggested_coa_account_id, confidence, resolution_action,
                        idempotency_key, created_at, embedding_model, embedding_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.tenant_id,
                        row.tx_id,
                        row.vendor_key,
                        row.vendor_raw,
                        row.amount,
                        row.currency,
                        txn_date,
                        txn_type,
                        row.final_coa_account_id,
                        row.suggested_coa_account_id,
                        row.confidence,
                        row.resolution_action,
                        row.idempotency_key,
                        created_at,
                        None,
                        None,
                    ),
                )
                return cur.rowcount == 1

    def list_by_tenant(
        self,
        tenant_id: str,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[RetrievalCorpusDocument]:
        """Returns corpus rows for one tenant, newest first.

        Args:
            tenant_id: Tenant scope.
            limit: Max rows to return (capped for API safety).
            offset: Pagination offset.

        Returns:
            Parsed corpus documents ordered by corpus_id descending.
        """
        cap = max(1, min(limit, 500))
        off = max(0, offset)

        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT corpus_id, tenant_id, tx_id, vendor_key, vendor_raw, amount, currency,
                           transaction_date, transaction_type, final_coa_account_id,
                           suggested_coa_account_id, confidence, resolution_action,
                           idempotency_key, created_at, embedding_model, embedding_version
                    FROM retrieval_corpus
                    WHERE tenant_id = ?
                    ORDER BY corpus_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (tenant_id, cap, off),
                ).fetchall()

        out: list[RetrievalCorpusDocument] = []
        for r in rows:
            txn_date_parsed: date | None = None
            if r[7] is not None:
                txn_date_parsed = date.fromisoformat(str(r[7]))
            txn_type_parsed = r[8]
            out.append(
                RetrievalCorpusDocument(
                    corpus_id=int(r[0]),
                    tenant_id=str(r[1]),
                    tx_id=str(r[2]),
                    vendor_key=str(r[3]),
                    vendor_raw=r[4],
                    amount=r[5],
                    currency=r[6],
                    transaction_date=txn_date_parsed,
                    transaction_type=txn_type_parsed,  # type: ignore[arg-type]
                    final_coa_account_id=str(r[9]),
                    suggested_coa_account_id=r[10],
                    confidence=float(r[11]) if r[11] is not None else None,
                    resolution_action=r[12],  # type: ignore[arg-type]
                    idempotency_key=str(r[13]),
                    created_at=datetime.fromisoformat(str(r[14])),
                    embedding_model=r[15],
                    embedding_version=int(r[16]) if r[16] is not None else None,
                )
            )
        return out

    def count_by_tenant(self, tenant_id: str) -> int:
        """Returns total corpus rows for one tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Row count.
        """
        with self._lock:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM retrieval_corpus WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()
        return int(row[0]) if row else 0
