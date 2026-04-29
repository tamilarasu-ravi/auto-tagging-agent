from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.models import TaggingResult
from app.store.idempotency_store import IdempotencyStore


def test_idempotency_store_persists_and_reads_cache_entries(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path)
    result = TaggingResult(
        tx_id="tx_902",
        tenant_id="tenant_a",
        status="UNKNOWN",
        source="unknown",
        coa_account_id=None,
        confidence=None,
        reasoning="no match",
        timestamp=datetime.now(timezone.utc),
        idempotency_key="idem_902",
    )
    store.put("tenant_a", "idem_902", "fingerprint-1", result)

    reloaded = IdempotencyStore(tmp_path)
    loaded = reloaded.get("tenant_a", "idem_902")

    assert loaded is not None
    fingerprint, cached = loaded
    assert fingerprint == "fingerprint-1"
    assert cached.tx_id == "tx_902"
