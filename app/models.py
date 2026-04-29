from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class Transaction(BaseModel):
    tx_id: str
    tenant_id: str
    vendor_raw: str
    amount: Decimal
    currency: str
    date: date
    transaction_type: Literal["card", "bill"]
    ocr_text: str | None = None
    idempotency_key: str


class CoAAccount(BaseModel):
    account_id: str
    name: str
    description: str
    parent_id: str | None = None


class LLMClassificationOutput(BaseModel):
    coa_account_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class TaggingResult(BaseModel):
    tx_id: str
    tenant_id: str
    status: Literal["AUTO_TAG", "REVIEW_QUEUE", "UNKNOWN"]
    source: Literal["rule", "llm", "unknown"]
    coa_account_id: str | None
    confidence: float | None
    reasoning: str | None
    timestamp: datetime
    idempotency_key: str


class VendorRule(BaseModel):
    tenant_id: str
    vendor_key: str
    coa_account_id: str
    created_by: Literal["reviewer", "import"]
    created_at: datetime
    source_tx_id: str | None = None
