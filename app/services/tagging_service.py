"""Orchestrates transaction tagging and review resolution (rule-first, then classifier)."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException

from app.adapters.accounting_sync import MockAccountingSyncAdapter
from app.config import AppConfig, TenantConfig
from app.models import (
    CoAAccount,
    ReviewQueueItem,
    ReviewResolveRequest,
    ReviewResolveResponse,
    RetrievalCorpusInsert,
    TaggingResult,
    Transaction,
    VendorRule,
)
from app.pipeline.llm_classifier import LLMClassifier
from app.pipeline.preprocessor import normalize_vendor, sanitize_free_text
from app.pipeline.router import route_by_confidence
from app.pipeline.validator import validate_classification_output
from app.store.audit_log import AuditLogStore
from app.store.confirmed_example_store import ConfirmedExampleStore
from app.store.idempotency_store import IdempotencyStore
from app.store.retrieval_corpus_store import RetrievalCorpusStore
from app.store.review_queue import ReviewQueueStore
from app.store.rule_store import RuleStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantRoutingThresholds:
    """Effective confidence routing thresholds for one tenant."""

    review_threshold: float
    auto_post_threshold: float


def _resolve_tenant_routing_thresholds(tenant_cfg: TenantConfig) -> TenantRoutingThresholds:
    """Computes routing thresholds, applying cold-start auto-post tightening when enabled."""
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


def _validate_resolution_replay_payload(
    *,
    existing_resolution: ReviewResolveResponse,
    request: ReviewResolveRequest,
) -> None:
    """Validates that repeated resolve calls use the same effective payload.

    Args:
        existing_resolution: Previously persisted resolution response for (tenant_id, tx_id).
        request: Incoming resolve request being replayed.

    Raises:
        HTTPException: If the replay request conflicts with the already-resolved payload.
    """
    existing_coa = existing_resolution.result.coa_account_id
    requested_action = request.action
    existing_action = "correct" if existing_resolution.rule_created else "accept"
    if existing_coa != request.final_coa_account_id or existing_action != requested_action:
        raise HTTPException(
            status_code=409,
            detail=(
                "Review item already resolved with a different payload; "
                "replay must use the same action and final_coa_account_id."
            ),
        )


class TaggingService:
    """Application use-case: tag transactions and resolve human review."""

    def __init__(
        self,
        *,
        app_config: AppConfig,
        coa_by_tenant: dict[str, list[CoAAccount]],
        coa_ids_by_tenant: dict[str, set[str]],
        rule_store: RuleStore,
        llm_classifier: LLMClassifier,
        audit_store: AuditLogStore,
        accounting_sync: MockAccountingSyncAdapter,
        idempotency_store: IdempotencyStore,
        review_queue_store: ReviewQueueStore,
        confirmed_example_store: ConfirmedExampleStore,
        retrieval_corpus_store: RetrievalCorpusStore,
        processing_lock: threading.RLock,
    ) -> None:
        self._app_config = app_config
        self._coa_by_tenant = coa_by_tenant
        self._coa_ids_by_tenant = coa_ids_by_tenant
        self._rule_store = rule_store
        self._llm_classifier = llm_classifier
        self._audit_store = audit_store
        self._accounting_sync = accounting_sync
        self._idempotency_store = idempotency_store
        self._review_queue_store = review_queue_store
        self._confirmed_example_store = confirmed_example_store
        self._retrieval_corpus_store = retrieval_corpus_store
        self._processing_lock = processing_lock

    def _ensure_tenant_exists(self, tenant_id: str) -> None:
        """Ensures tenant-scoped requests fail safely when tenant is unknown.

        Args:
            tenant_id: Tenant identifier from the incoming request.

        Raises:
            HTTPException: When tenant_id is not present in the loaded application config.
        """
        if tenant_id not in self._app_config.tenants:
            raise HTTPException(status_code=404, detail="Unknown tenant_id.")

    def tag_transaction(self, transaction: Transaction) -> TaggingResult:
        """Runs rule-first tagging, then classifier routing, with idempotency and audit."""
        self._ensure_tenant_exists(transaction.tenant_id)
        payload_fingerprint = _transaction_fingerprint(transaction)
        with self._processing_lock:
            cached = self._idempotency_store.get(transaction.tenant_id, transaction.idempotency_key)
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
            rule = self._rule_store.match(transaction.tenant_id, vendor_key) if vendor_key else None

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
                self._audit_store.append(result)
                self._accounting_sync.sync(result)
                logger.info(
                    "AUTO_TAG via rule tenant=%s tx=%s vendor_key=%s coa=%s",
                    transaction.tenant_id,
                    transaction.tx_id,
                    vendor_key,
                    rule.coa_account_id,
                )
            else:
                tenant_id = transaction.tenant_id
                tenant_coa = self._coa_by_tenant[tenant_id]
                tenant_config = self._app_config.tenants[tenant_id]
                routing_thresholds = _resolve_tenant_routing_thresholds(tenant_config)
                classification_result = self._llm_classifier.classify(
                    transaction,
                    tenant_coa,
                    tenant_name=tenant_config.tenant_name,
                    few_shot_examples=self._confirmed_example_store.sample_examples(
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
                    self._audit_store.append(result)
                    logger.warning(
                        "UNKNOWN after classifier failure tenant=%s tx=%s reason=%s",
                        tenant_id,
                        transaction.tx_id,
                        classification_result.error_reason,
                    )
                else:
                    classification = classification_result.output
                    valid_coa_ids = self._coa_ids_by_tenant[tenant_id]
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
                        self._audit_store.append(result)
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
                        sanitized_reasoning = sanitize_free_text(classification.reasoning) or ""
                        result = TaggingResult(
                            tx_id=transaction.tx_id,
                            tenant_id=tenant_id,
                            status=status,
                            source="llm" if status in {"AUTO_TAG", "REVIEW_QUEUE"} else "unknown",
                            coa_account_id=classification.coa_account_id if status != "UNKNOWN" else None,
                            confidence=classification.confidence if status != "UNKNOWN" else None,
                            reasoning=sanitized_reasoning,
                            timestamp=datetime.now(timezone.utc),
                            idempotency_key=transaction.idempotency_key,
                            provider_name=classification_result.provider_name,
                            latency_ms=classification_result.latency_ms,
                            prompt_tokens=classification_result.prompt_tokens,
                            completion_tokens=classification_result.completion_tokens,
                            total_tokens=classification_result.total_tokens,
                        )
                        self._audit_store.append(result)
                        if status == "AUTO_TAG":
                            self._accounting_sync.sync(result)
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
                            self._review_queue_store.add(
                                ReviewQueueItem(
                                    tx_id=transaction.tx_id,
                                    tenant_id=tenant_id,
                                    vendor_key=vendor_key,
                                    suggested_coa_account_id=classification.coa_account_id,
                                    confidence=classification.confidence,
                                    reasoning=sanitized_reasoning,
                                    idempotency_key=transaction.idempotency_key,
                                    vendor_raw=transaction.vendor_raw,
                                    amount=str(transaction.amount),
                                    currency=transaction.currency,
                                    transaction_date=transaction.date,
                                    transaction_type=transaction.transaction_type,
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

            self._idempotency_store.put(
                transaction.tenant_id,
                transaction.idempotency_key,
                payload_fingerprint,
                result,
            )
            return result

    def resolve_review_item(self, tx_id: str, request: ReviewResolveRequest) -> ReviewResolveResponse:
        """Resolves a queued review item; may promote a deterministic vendor rule."""
        self._ensure_tenant_exists(request.tenant_id)
        valid_coa_ids = self._coa_ids_by_tenant[request.tenant_id]
        if request.final_coa_account_id not in valid_coa_ids:
            raise HTTPException(status_code=422, detail="final_coa_account_id is not in tenant CoA.")

        with self._processing_lock:
            existing_resolution = self._review_queue_store.get_resolution(request.tenant_id, tx_id)
            if existing_resolution is not None:
                _validate_resolution_replay_payload(
                    existing_resolution=existing_resolution,
                    request=request,
                )
                return existing_resolution

            queued_item = self._review_queue_store.resolve(request.tenant_id, tx_id)
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
                    sanitize_free_text(queued_item.reasoning) or ""
                    if request.action == "accept"
                    else sanitize_free_text(
                        f"Reviewer corrected suggestion to {request.final_coa_account_id}."
                    )
                    or ""
                ),
                timestamp=datetime.now(timezone.utc),
                idempotency_key=queued_item.idempotency_key,
            )
            self._audit_store.append(resolved_result)
            self._accounting_sync.sync(resolved_result)
            self._confirmed_example_store.add_example(
                request.tenant_id,
                queued_item.vendor_key,
                {
                    "vendor_key": queued_item.vendor_key,
                    "coa_account_id": request.final_coa_account_id,
                    "action": request.action,
                },
            )
            corpus_row = RetrievalCorpusInsert(
                tenant_id=request.tenant_id,
                tx_id=queued_item.tx_id,
                vendor_key=queued_item.vendor_key,
                vendor_raw=queued_item.vendor_raw,
                amount=queued_item.amount,
                currency=queued_item.currency,
                transaction_date=queued_item.transaction_date,
                transaction_type=queued_item.transaction_type,
                final_coa_account_id=request.final_coa_account_id,
                suggested_coa_account_id=queued_item.suggested_coa_account_id,
                confidence=queued_item.confidence,
                resolution_action=request.action,
                idempotency_key=queued_item.idempotency_key,
            )
            self._retrieval_corpus_store.insert(corpus_row)
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
                self._rule_store.upsert_rule(promoted_rule)
                rule_created = True

            response = ReviewResolveResponse(
                result=resolved_result,
                rule_created=rule_created,
                resolved_at=datetime.now(timezone.utc),
                resolved_by=request.reviewer_id,
            )
            self._review_queue_store.save_resolution(request.tenant_id, tx_id, response)
            logger.info(
                "review resolved tenant=%s tx=%s action=%s rule_created=%s",
                request.tenant_id,
                tx_id,
                request.action,
                rule_created,
            )
            return response
