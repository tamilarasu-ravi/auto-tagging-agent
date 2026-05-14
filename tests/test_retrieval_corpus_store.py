"""Unit tests for RetrievalCorpusStore."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

from app.models import RetrievalCorpusInsert
from app.store.retrieval_corpus_store import RetrievalCorpusStore


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / f"corpus_{uuid4().hex}.db"


def test_insert_and_list_round_trip(tmp_path: Path) -> None:
    """Insert persists fields and list_by_tenant returns RetrievalCorpusDocument rows."""
    store = RetrievalCorpusStore(_db_path(tmp_path))
    row = RetrievalCorpusInsert(
        tenant_id="tenant_a",
        tx_id="tx_corpus_1",
        vendor_key="grab sg 1",
        vendor_raw="Grab SG 1",
        amount="18.50",
        currency="SGD",
        transaction_date=date(2026, 4, 30),
        transaction_type="card",
        final_coa_account_id="7200",
        suggested_coa_account_id="7200",
        confidence=0.75,
        resolution_action="accept",
        idempotency_key="idem_corpus_1",
    )
    assert store.insert(row) is True
    docs = store.list_by_tenant("tenant_a")
    assert len(docs) == 1
    d = docs[0]
    assert d.tx_id == "tx_corpus_1"
    assert d.final_coa_account_id == "7200"
    assert d.vendor_raw == "Grab SG 1"
    assert d.amount == "18.50"
    assert d.currency == "SGD"
    assert d.transaction_date == date(2026, 4, 30)
    assert d.transaction_type == "card"
    assert d.resolution_action == "accept"


def test_insert_ignore_duplicate_tenant_tx(tmp_path: Path) -> None:
    """UNIQUE (tenant_id, tx_id) causes second insert to be ignored."""
    store = RetrievalCorpusStore(_db_path(tmp_path))
    base = RetrievalCorpusInsert(
        tenant_id="tenant_a",
        tx_id="tx_dup",
        vendor_key="v",
        final_coa_account_id="6100",
        suggested_coa_account_id="6100",
        confidence=0.9,
        resolution_action="correct",
        idempotency_key="k1",
    )
    assert store.insert(base) is True
    second = base.model_copy(update={"final_coa_account_id": "6200", "idempotency_key": "k2"})
    assert store.insert(second) is False
    assert store.count_by_tenant("tenant_a") == 1
    assert store.list_by_tenant("tenant_a")[0].final_coa_account_id == "6100"


def test_list_by_tenant_is_scoped(tmp_path: Path) -> None:
    """Rows for tenant_a are not returned for tenant_b."""
    store = RetrievalCorpusStore(_db_path(tmp_path))
    store.insert(
        RetrievalCorpusInsert(
            tenant_id="tenant_a",
            tx_id="tx_a",
            vendor_key="a",
            vendor_raw=None,
            final_coa_account_id="6100",
            suggested_coa_account_id="6100",
            confidence=0.9,
            resolution_action="accept",
            idempotency_key="ia",
        )
    )
    store.insert(
        RetrievalCorpusInsert(
            tenant_id="tenant_b",
            tx_id="tx_b",
            vendor_key="b",
            final_coa_account_id="5050",
            suggested_coa_account_id="5050",
            confidence=0.91,
            resolution_action="accept",
            idempotency_key="ib",
        )
    )
    assert len(store.list_by_tenant("tenant_a")) == 1
    assert len(store.list_by_tenant("tenant_b")) == 1
    assert store.list_by_tenant("tenant_a")[0].tenant_id == "tenant_a"
