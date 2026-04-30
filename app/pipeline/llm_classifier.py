"""Tenant-scoped LLM classification with provider fallback; orchestrates prompt, provider, and fallback."""

from __future__ import annotations

import logging
import time
from typing import Callable

from app.models import CoAAccount, Transaction
from app.pipeline.llm_fallback import classify_transaction_no_llm
from app.pipeline.llm_prompt import build_classification_messages
from app.pipeline.llm_provider import (
    build_provider_chain_from_env,
    default_completion_fn,
    extract_status_code,
    extract_usage,
    parse_response_output,
)
from app.pipeline.llm_types import CompletionFn, LLMClassificationResult, ProviderConfig

logger = logging.getLogger(__name__)


class LLMClassifier:
    """Runs tenant-scoped classification with provider fallback semantics."""

    def __init__(
        self,
        provider_chain: list[ProviderConfig] | None = None,
        completion_fn: CompletionFn | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._provider_chain = (
            provider_chain if provider_chain is not None else build_provider_chain_from_env()
        )
        self._completion_fn = completion_fn
        self._sleep_fn = sleep_fn or time.sleep
        self._time_fn = time_fn or time.monotonic

    def classify(
        self,
        transaction: Transaction,
        tenant_coa: list[CoAAccount],
        tenant_name: str,
        few_shot_examples: list[dict[str, object]] | None = None,
        timeout_budget_s: float = 15.0,
    ) -> LLMClassificationResult:
        """Classifies a transaction using fallback chain or deterministic fallback when no providers exist."""
        if not self._provider_chain:
            return LLMClassificationResult(
                output=classify_transaction_no_llm(transaction, tenant_coa),
                provider_name="deterministic_fallback",
                error_reason=None,
            )

        deadline = self._time_fn() + timeout_budget_s
        messages = build_classification_messages(
            transaction, tenant_coa, tenant_name, few_shot_examples or []
        )
        completion_fn = self._completion_fn or default_completion_fn

        for provider in self._provider_chain:
            retry_count = 0
            while True:
                now = self._time_fn()
                if now >= deadline:
                    logger.warning(
                        "LLM classification deadline exceeded tx=%s tenant=%s",
                        transaction.tx_id,
                        transaction.tenant_id,
                    )
                    return LLMClassificationResult(
                        output=None,
                        provider_name=None,
                        error_reason="deadline_exceeded",
                    )

                try:
                    timeout_s = max(0.1, min(8.0, deadline - now))
                    call_started = self._time_fn()
                    response = completion_fn(
                        model=provider.model,
                        messages=messages,
                        temperature=0,
                        timeout=timeout_s,
                    )
                    output = parse_response_output(response)
                    usage = extract_usage(response)
                    latency_ms = (self._time_fn() - call_started) * 1000.0
                    return LLMClassificationResult(
                        output=output,
                        provider_name=provider.name,
                        error_reason=None,
                        latency_ms=latency_ms,
                        prompt_tokens=usage.get("prompt_tokens"),
                        completion_tokens=usage.get("completion_tokens"),
                        total_tokens=usage.get("total_tokens"),
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    status_code = extract_status_code(exc)
                    if status_code == 429:
                        if retry_count < 2:
                            retry_count += 1
                            self._sleep_fn(0.25 * (2**retry_count))
                            continue
                        break
                    if status_code is not None and 400 <= status_code < 500:
                        logger.warning(
                            "LLM provider 4xx (no fallback) provider=%s tx=%s status=%s",
                            provider.name,
                            transaction.tx_id,
                            status_code,
                        )
                        return LLMClassificationResult(
                            output=None,
                            provider_name=provider.name,
                            error_reason="provider_4xx",
                        )
                    break

        logger.warning(
            "LLM providers exhausted or unreachable tx=%s tenant=%s",
            transaction.tx_id,
            transaction.tenant_id,
        )
        return LLMClassificationResult(
            output=None,
            provider_name=None,
            error_reason="providers_exhausted",
        )
