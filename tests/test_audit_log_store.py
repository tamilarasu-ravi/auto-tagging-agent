from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.models import TaggingResult
from app.store.audit_log import AuditLogStore


def test_audit_log_store_persists_records_to_disk(tmp_path: Path) -> None:
    store = AuditLogStore(tmp_path)
    item = TaggingResult(
        tx_id="tx_901",
        tenant_id="tenant_a",
        status="UNKNOWN",
        source="unknown",
        coa_account_id=None,
        confidence=None,
        reasoning="no match",
        timestamp=datetime.now(timezone.utc),
        idempotency_key="idem_901",
    )
    store.append(item)

    reloaded = AuditLogStore(tmp_path)
    values = reloaded.list_by_tenant("tenant_a")

    assert len(values) == 1
    assert values[0].tx_id == "tx_901"
