from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException

from app.adapters.accounting_sync import MockAccountingSyncAdapter
from app.config import AppConfig, TenantConfig, load_app_config
from app.models import (
    CoAAccount,
    ReviewQueueItem,
    ReviewResolveRequest,
    ReviewResolveResponse,
    VendorRule,
    TaggingResult,
    Transaction,
)
from app.pipeline.llm_classifier import LLMClassifier
from app.pipeline.preprocessor import normalize_vendor
from app.pipeline.router import route_by_confidence
from app.pipeline.validator import validate_classification_output
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
logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class TenantRoutingThresholds:
    """Represents effective confidence routing thresholds for one tenant."""

    review_threshold: float
    auto_post_threshold: float


def _resolve_tenant_routing_thresholds(tenant_cfg: TenantConfig) -> TenantRoutingThresholds:
    """Computes routing thresholds, applying cold-start auto-post tightening when enabled.

    Args:
        tenant_cfg: Loaded tenant configuration model.

    Returns:
        Effective routing thresholds used by the confidence router.
    """
    cold_start_auto_post = 0.95
    auto_post = cold_start_auto_post if tenant_cfg.cold_start else tenant_cfg.auto_post_threshold
    return TenantRoutingThresholds(
        review_threshold=tenant_cfg.review_threshold,
        auto_post_threshold=auto_post,
    )


def _transaction_fingerprint(transaction: Transaction) -> str:
    """Builds a stable hash for idempotency payload conflict detection."""
    payload = json.dumps(transaction.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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

    payload_fingerprint = _transaction_fingerprint(transaction)
    with processing_lock:
        cached = idempotency_store.get(transaction.tenant_id, transaction.idempotency_key)
        if cached:
            cached_fingerprint, cached_result = cached
            if cached_fingerprint != payload_fingerprint:
                logger.warning(
                    "idempotency conflict tenant=%s tx=%s key=%s",
                    transaction.tenant_id,
                    transaction.tx_id,
                    transaction.idempotency_key,
                )
                raise HTTPException(
                    status_code=409,
                    detail="idempotency_key already used with a different payload.",
                )
            logger.info(
                "idempotency cache hit tenant=%s tx=%s key=%s",
                transaction.tenant_id,
                transaction.tx_id,
                transaction.idempotency_key,
            )
            return cached_result

        vendor_key = normalize_vendor(transaction.vendor_raw)
        rule = rule_store.match(transaction.tenant_id, vendor_key) if vendor_key else None

        if rule:
            result = TaggingResult(
                tx_id=transaction.tx_id,
                tenant_id=transaction.tenant_id,
                status="AUTO_TAG",
                source="rule",
                coa_account_id=rule.coa_account_id,
                confidence=1.0,
                reasoning=f"Deterministic vendor rule matched for '{vendor_key}'.",
                timestamp=datetime.now(timezone.utc),
                idempotency_key=transaction.idempotency_key,
            )
            audit_store.append(result)
            accounting_sync.sync(result)
            logger.info(
                "AUTO_TAG via rule tenant=%s tx=%s vendor_key=%s coa=%s",
                transaction.tenant_id,
                transaction.tx_id,
                vendor_key,
                rule.coa_account_id,
            )
        else:
            tenant_id = transaction.tenant_id
            tenant_coa = coa_by_tenant[tenant_id]
            tenant_config = app_config.tenants[tenant_id]
            routing_thresholds = _resolve_tenant_routing_thresholds(tenant_config)
            classification_result = llm_classifier.classify(
                transaction,
                tenant_coa,
                tenant_name=tenant_config.tenant_name,
                few_shot_examples=confirmed_example_store.sample_examples(
                    tenant_id,
                    exclude_vendor_key=vendor_key,
                    tx_id=transaction.tx_id,
                    limit=5,
                ),
            )

            if classification_result.output is None:
                result = TaggingResult(
                    tx_id=transaction.tx_id,
                    tenant_id=tenant_id,
                    status="UNKNOWN",
                    source="unknown",
                    coa_account_id=None,
                    confidence=0.0,
                    reasoning=f"LLM classification unavailable: {classification_result.error_reason}.",
                    timestamp=datetime.now(timezone.utc),
                    idempotency_key=transaction.idempotency_key,
                    provider_name=classification_result.provider_name,
                    latency_ms=classification_result.latency_ms,
                    prompt_tokens=classification_result.prompt_tokens,
                    completion_tokens=classification_result.completion_tokens,
                    total_tokens=classification_result.total_tokens,
                )
                audit_store.append(result)
                logger.warning(
                    "UNKNOWN after classifier failure tenant=%s tx=%s reason=%s",
                    tenant_id,
                    transaction.tx_id,
                    classification_result.error_reason,
                )
            else:
                classification = classification_result.output
                valid_coa_ids = coa_ids_by_tenant[tenant_id]
                is_valid = validate_classification_output(classification, valid_coa_ids)
                if not is_valid:
                    result = TaggingResult(
                        tx_id=transaction.tx_id,
                        tenant_id=tenant_id,
                        status="UNKNOWN",
                        source="unknown",
                        coa_account_id=None,
                        confidence=None,
                        reasoning="Classifier output account is outside tenant CoA.",
                        timestamp=datetime.now(timezone.utc),
                        idempotency_key=transaction.idempotency_key,
                    )
                    audit_store.append(result)
                    logger.warning(
                        "UNKNOWN invalid CoA from classifier tenant=%s tx=%s",
                        tenant_id,
                        transaction.tx_id,
                    )
                else:
                    status = route_by_confidence(
                        classification.confidence,
                        review_threshold=routing_thresholds.review_threshold,
                        auto_post_threshold=routing_thresholds.auto_post_threshold,
                    )
                    result = TaggingResult(
                        tx_id=transaction.tx_id,
                        tenant_id=tenant_id,
                        status=status,
                        source="llm" if status in {"AUTO_TAG", "REVIEW_QUEUE"} else "unknown",
                        coa_account_id=classification.coa_account_id if status != "UNKNOWN" else None,
                        confidence=classification.confidence if status != "UNKNOWN" else None,
                        reasoning=classification.reasoning,
                        timestamp=datetime.now(timezone.utc),
                        idempotency_key=transaction.idempotency_key,
                        provider_name=classification_result.provider_name,
                        latency_ms=classification_result.latency_ms,
                        prompt_tokens=classification_result.prompt_tokens,
                        completion_tokens=classification_result.completion_tokens,
                        total_tokens=classification_result.total_tokens,
                    )
                    audit_store.append(result)
                    if status == "AUTO_TAG":
                        accounting_sync.sync(result)
                        logger.info(
                            "AUTO_TAG via classifier tenant=%s tx=%s coa=%s conf=%.2f "
                            "thresholds review=%.2f auto_post=%.2f",
                            tenant_id,
                            transaction.tx_id,
                            classification.coa_account_id,
                            classification.confidence,
                            routing_thresholds.review_threshold,
                            routing_thresholds.auto_post_threshold,
                        )
                    if status == "REVIEW_QUEUE":
                        logger.info(
                            "[Action] Routed to human review tenant=%s tx=%s confidence=%.2f "
                            "(review_threshold=%.2f auto_post_threshold=%.2f)",
                            tenant_id,
                            transaction.tx_id,
                            classification.confidence,
                            routing_thresholds.review_threshold,
                            routing_thresholds.auto_post_threshold,
                        )
                        review_queue_store.add(
                            ReviewQueueItem(
                                tx_id=transaction.tx_id,
                                tenant_id=tenant_id,
                                vendor_key=vendor_key,
                                suggested_coa_account_id=classification.coa_account_id,
                                confidence=classification.confidence,
                                reasoning=classification.reasoning,
                                idempotency_key=transaction.idempotency_key,
                            )
                        )
                    if status == "UNKNOWN":
                        logger.info(
                            "[Action] Refused auto-post (low confidence or below review bar) "
                            "tenant=%s tx=%s confidence=%.2f",
                            tenant_id,
                            transaction.tx_id,
                            classification.confidence,
                        )

        idempotency_store.put(
            transaction.tenant_id,
            transaction.idempotency_key,
            payload_fingerprint,
            result,
        )
        return result


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

    valid_coa_ids = coa_ids_by_tenant[request.tenant_id]
    if request.final_coa_account_id not in valid_coa_ids:
        raise HTTPException(status_code=422, detail="final_coa_account_id is not in tenant CoA.")

    with processing_lock:
        existing_resolution = review_queue_store.get_resolution(request.tenant_id, tx_id)
        if existing_resolution is not None:
            return existing_resolution

        queued_item = review_queue_store.resolve(request.tenant_id, tx_id)
        if queued_item is None:
            raise HTTPException(status_code=404, detail="Review item not found.")

        resolved_result = TaggingResult(
            tx_id=queued_item.tx_id,
            tenant_id=queued_item.tenant_id,
            status="AUTO_TAG",
            source="llm",
            coa_account_id=request.final_coa_account_id,
            confidence=queued_item.confidence,
            reasoning=(
                queued_item.reasoning
                if request.action == "accept"
                else f"Reviewer corrected suggestion to {request.final_coa_account_id}."
            ),
            timestamp=datetime.now(timezone.utc),
            idempotency_key=queued_item.idempotency_key,
        )
        audit_store.append(resolved_result)
        accounting_sync.sync(resolved_result)
        confirmed_example_store.add_example(
            request.tenant_id,
            queued_item.vendor_key,
            {
                "vendor_key": queued_item.vendor_key,
                "coa_account_id": request.final_coa_account_id,
                "action": request.action,
            },
        )
        rule_created = False
        if request.action == "correct":
            promoted_rule = VendorRule(
                tenant_id=request.tenant_id,
                vendor_key=queued_item.vendor_key,
                coa_account_id=request.final_coa_account_id,
                created_by="reviewer",
                created_at=datetime.now(timezone.utc),
                source_tx_id=tx_id,
            )
            rule_store.upsert_rule(promoted_rule)
            rule_created = True

        response = ReviewResolveResponse(
            result=resolved_result,
            rule_created=rule_created,
            resolved_at=datetime.now(timezone.utc),
            resolved_by=request.reviewer_id,
        )
        review_queue_store.save_resolution(request.tenant_id, tx_id, response)
        logger.info(
            "review resolved tenant=%s tx=%s action=%s rule_created=%s",
            request.tenant_id,
            tx_id,
            request.action,
            rule_created,
        )
        return response


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
