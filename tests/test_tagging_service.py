from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.main import tagging_service
from app.models import ReviewResolveRequest, Transaction


def test_tagging_service_unknown_tenant_returns_404() -> None:
    tx = Transaction(
        tx_id="tx_unknown_tenant",
        tenant_id="tenant_x",
        vendor_raw="Zoom US",
        amount="10.00",
        currency="USD",
        date="2026-04-30",
        transaction_type="card",
        idempotency_key="idem_unknown_tenant",
    )

    with pytest.raises(HTTPException) as excinfo:
        tagging_service.tag_transaction(tx)

    assert excinfo.value.status_code == 404


def test_tagging_service_resolve_unknown_tenant_returns_404() -> None:
    request = ReviewResolveRequest(
        tenant_id="tenant_x",
        action="accept",
        final_coa_account_id="6100",
        reviewer_id="reviewer_001",
    )

    with pytest.raises(HTTPException) as excinfo:
        tagging_service.resolve_review_item("tx_001", request)

    assert excinfo.value.status_code == 404

