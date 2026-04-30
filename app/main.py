from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException

from app.adapters.accounting_sync import MockAccountingSyncAdapter
from app.config import AppConfig, load_app_config
from app.models import (
    CoAAccount,
    ReviewResolveRequest,
    ReviewResolveResponse,
    TaggingResult,
    Transaction,
)
from app.pipeline.llm_classifier import LLMClassifier
from app.services.tagging_service import TaggingService
from app.store.audit_log import AuditLogStore
from app.store.confirmed_example_store import ConfirmedExampleStore
from app.store.idempotency_store import IdempotencyStore
from app.store.review_queue import ReviewQueueStore
from app.store.rule_store import RuleStore


APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_ROOT / "data" / "tenants.json"
RUNTIME_DIR = APP_ROOT / "data" / "runtime"
STATE_DB_PATH = RUNTIME_DIR / "state.db"

if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI(title="Reap CFO Agent", version="0.1.0")
app_config: AppConfig = load_app_config(CONFIG_PATH)
audit_store = AuditLogStore(STATE_DB_PATH)
accounting_sync = MockAccountingSyncAdapter()
idempotency_store = IdempotencyStore(STATE_DB_PATH)
review_queue_store = ReviewQueueStore(STATE_DB_PATH)
confirmed_example_store = ConfirmedExampleStore(STATE_DB_PATH)
processing_lock = threading.RLock()

coa_by_tenant: dict[str, list[CoAAccount]] = {}
coa_ids_by_tenant: dict[str, set[str]] = {}
rules_paths: dict[str, str] = {}
api_keys_by_tenant: dict[str, str] = {}
for configured_tenant_id, tenant_cfg in app_config.tenants.items():
    coa_payload = json.loads((APP_ROOT / tenant_cfg.coa_path).read_text(encoding="utf-8"))
    coa_by_tenant[configured_tenant_id] = [CoAAccount(**item) for item in coa_payload]
    coa_ids_by_tenant[configured_tenant_id] = {
        item.account_id for item in coa_by_tenant[configured_tenant_id]
    }
    rules_paths[configured_tenant_id] = tenant_cfg.rules_path
    api_keys_by_tenant[configured_tenant_id] = tenant_cfg.api_key

rule_store = RuleStore(APP_ROOT, rules_paths, coa_ids_by_tenant)
llm_classifier = LLMClassifier()

tagging_service = TaggingService(
    app_config=app_config,
    coa_by_tenant=coa_by_tenant,
    coa_ids_by_tenant=coa_ids_by_tenant,
    rule_store=rule_store,
    llm_classifier=llm_classifier,
    audit_store=audit_store,
    accounting_sync=accounting_sync,
    idempotency_store=idempotency_store,
    review_queue_store=review_queue_store,
    confirmed_example_store=confirmed_example_store,
    processing_lock=processing_lock,
)


def _authorize_tenant_request(tenant_id: str, api_key: str | None) -> None:
    """Authorizes tenant-scoped requests using static per-tenant API keys."""
    expected_key = api_keys_by_tenant.get(tenant_id)
    if expected_key is None:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")
    if not api_key or api_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid API key for tenant.")


@app.get("/health")
def health() -> dict[str, str]:
    """Returns a basic service health response."""
    return {"status": "ok", "service": "reap-cfo-agent"}


@app.post("/transactions/tag")
def tag_transaction(
    transaction: Transaction,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TaggingResult:
    """Tags one transaction with rule-first then classifier routing."""
    _authorize_tenant_request(transaction.tenant_id, x_api_key)
    return tagging_service.tag_transaction(transaction)


@app.get("/review-queue/{tenant_id}")
def get_review_queue(
    tenant_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict[str, object]]:
    """Returns pending review queue items for one tenant."""
    _authorize_tenant_request(tenant_id, x_api_key)
    return [
        {
            "tx_id": item.tx_id,
            "coa_account_id": item.suggested_coa_account_id,
            "confidence": item.confidence,
            "reasoning": item.reasoning,
        }
        for item in review_queue_store.list_by_tenant(tenant_id)
    ]


@app.post("/review-queue/{tx_id}/resolve")
def resolve_review_item(
    tx_id: str,
    request: ReviewResolveRequest,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ReviewResolveResponse:
    """Resolves a review queue item by accepting or correcting the suggested account."""
    _authorize_tenant_request(request.tenant_id, x_api_key)
    return tagging_service.resolve_review_item(tx_id, request)


@app.get("/audit-log/{tenant_id}")
def get_audit_log(
    tenant_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[TaggingResult]:
    """Returns tenant-scoped audit events."""
    _authorize_tenant_request(tenant_id, x_api_key)
    return audit_store.list_by_tenant(tenant_id)


@app.get("/rules/{tenant_id}")
def get_rules(
    tenant_id: str,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> list[dict[str, object]]:
    """Returns current deterministic rules for one tenant."""
    _authorize_tenant_request(tenant_id, x_api_key)
    return [rule.model_dump(mode="json") for rule in rule_store.list_rules(tenant_id)]
