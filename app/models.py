from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

MAX_VENDOR_RAW_LEN = 500
MAX_OCR_TEXT_LEN = 2000


class Transaction(BaseModel):
    tx_id: str
    tenant_id: str
    vendor_raw: str = Field(min_length=1, max_length=MAX_VENDOR_RAW_LEN)
    amount: Decimal
    currency: str
    date: date
    transaction_type: Literal["card", "bill"]
    ocr_text: str | None = Field(default=None, max_length=MAX_OCR_TEXT_LEN)
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
    provider_name: str | None = None
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class VendorRule(BaseModel):
    tenant_id: str
    vendor_key: str
    coa_account_id: str
    created_by: Literal["reviewer", "import"]
    created_at: datetime
    source_tx_id: str | None = None


class ReviewResolveRequest(BaseModel):
    tenant_id: str
    action: Literal["accept", "correct"]
    final_coa_account_id: str
    reviewer_id: str | None = None


class ReviewResolveResponse(BaseModel):
    result: TaggingResult
    rule_created: bool
    resolved_at: datetime
    resolved_by: str | None = None


class ReviewQueueItem(BaseModel):
    """Pending human review for one transaction (includes optional fields for Phase 1 retrieval corpus)."""

    tx_id: str
    tenant_id: str
    vendor_key: str
    suggested_coa_account_id: str
    confidence: float
    reasoning: str
    idempotency_key: str
    vendor_raw: str | None = Field(default=None, max_length=MAX_VENDOR_RAW_LEN)
    amount: str | None = Field(default=None, description="Decimal amount string from original transaction.")
    currency: str | None = Field(default=None, max_length=8)
    transaction_date: date | None = Field(default=None, description="Spend date from original transaction.")
    transaction_type: Literal["card", "bill"] | None = Field(default=None)


class RetrievalCorpusDocument(BaseModel):
    """One persisted human-confirmed label row for future retrieval / embedding (Phase 1 store)."""

    corpus_id: int
    tenant_id: str
    tx_id: str
    vendor_key: str
    vendor_raw: str | None = None
    amount: str | None = None
    currency: str | None = None
    transaction_date: date | None = None
    transaction_type: Literal["card", "bill"] | None = None
    final_coa_account_id: str
    suggested_coa_account_id: str | None = None
    confidence: float | None = None
    resolution_action: Literal["accept", "correct"]
    idempotency_key: str
    created_at: datetime
    embedding_model: str | None = None
    embedding_version: int | None = None


class RetrievalCorpusInsert(BaseModel):
    """Payload to append one retrieval corpus row after a successful review resolve."""

    tenant_id: str
    tx_id: str
    vendor_key: str
    vendor_raw: str | None = None
    amount: str | None = None
    currency: str | None = None
    transaction_date: date | None = None
    transaction_type: Literal["card", "bill"] | None = None
    final_coa_account_id: str
    suggested_coa_account_id: str | None = None
    confidence: float | None = None
    resolution_action: Literal["accept", "correct"]
    idempotency_key: str
