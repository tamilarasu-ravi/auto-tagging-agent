from __future__ import annotations

from app.models import CoAAccount, LLMClassificationOutput, Transaction


def classify_transaction_no_llm(
    transaction: Transaction,
    tenant_coa: list[CoAAccount],
) -> LLMClassificationOutput:
    """Returns a deterministic classifier output for Step 3 without external LLM calls.

    Args:
        transaction: Incoming transaction payload.
        tenant_coa: Tenant-scoped chart-of-accounts list.

    Returns:
        A structured classification output compatible with the validator/router pipeline.
    """
    vendor_lower = transaction.vendor_raw.lower()
    _ = tenant_coa

    if "aws" in vendor_lower:
        return LLMClassificationOutput(
            coa_account_id="6200",
            confidence=0.93,
            reasoning="Vendor resembles cloud infrastructure spend.",
        )
    if "grab" in vendor_lower:
        return LLMClassificationOutput(
            coa_account_id="7200",
            confidence=0.65,
            reasoning="Vendor resembles ride-hailing or local transport.",
        )
    if "pttep" in vendor_lower:
        return LLMClassificationOutput(
            coa_account_id="6200",
            confidence=0.31,
            reasoning="Vendor is ambiguous and should be routed conservatively.",
        )

    return LLMClassificationOutput(
        coa_account_id="6200",
        confidence=0.25,
        reasoning="Insufficient deterministic signal in core-no-llm mode.",
    )
