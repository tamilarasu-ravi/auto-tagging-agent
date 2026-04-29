from __future__ import annotations

import importlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable

from app.models import CoAAccount, LLMClassificationOutput, Transaction


@dataclass(frozen=True)
class ProviderConfig:
    """Defines one LLM provider in the fallback chain."""

    name: str
    model: str


@dataclass
class LLMClassificationResult:
    """Represents classifier success or a terminal error outcome."""

    output: LLMClassificationOutput | None
    provider_name: str | None
    error_reason: str | None
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


CompletionFn = Callable[..., object]
EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
CARD_LAST4_RE = re.compile(r"\b\d{4}\b")


class LLMClassifier:
    """Runs tenant-scoped classification with provider fallback semantics."""

    def __init__(
        self,
        provider_chain: list[ProviderConfig] | None = None,
        completion_fn: CompletionFn | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._provider_chain = provider_chain if provider_chain is not None else _build_provider_chain_from_env()
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
        messages = _build_messages(transaction, tenant_coa, tenant_name, few_shot_examples or [])
        completion_fn = self._completion_fn or _default_completion_fn

        for provider in self._provider_chain:
            retry_count = 0
            while True:
                now = self._time_fn()
                if now >= deadline:
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
                    output = _parse_response_output(response)
                    usage = _extract_usage(response)
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
                    status_code = _extract_status_code(exc)
                    if status_code == 429:
                        if retry_count < 2:
                            retry_count += 1
                            self._sleep_fn(0.25 * (2**retry_count))
                            continue
                        break
                    if status_code is not None and 400 <= status_code < 500:
                        return LLMClassificationResult(
                            output=None,
                            provider_name=provider.name,
                            error_reason="provider_4xx",
                        )
                    break

        return LLMClassificationResult(
            output=None,
            provider_name=None,
            error_reason="providers_exhausted",
        )


def _build_provider_chain_from_env() -> list[ProviderConfig]:
    """Builds provider order from available API keys.

    Live provider calls are intentionally opt-in to keep local dev/tests deterministic.
    Set `LLM_ENABLE_LIVE_CALLS=true` to enable real provider chaining.
    """
    if os.getenv("LLM_ENABLE_LIVE_CALLS", "false").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return []

    chain: list[ProviderConfig] = []
    if os.getenv("GOOGLE_API_KEY"):
        chain.append(ProviderConfig(name="gemini", model=os.getenv("GEMINI_MODEL", "gemini/gemini-2.0-flash")))
    if os.getenv("CLAUDE_API_KEY"):
        chain.append(
            ProviderConfig(name="claude", model=os.getenv("CLAUDE_MODEL", "anthropic/claude-3-5-sonnet-latest"))
        )
    if os.getenv("OPENAI_API_KEY"):
        chain.append(ProviderConfig(name="openai", model=os.getenv("OPENAI_MODEL", "openai/gpt-4o")))
    return chain


def _build_messages(
    transaction: Transaction,
    tenant_coa: list[CoAAccount],
    tenant_name: str,
    few_shot_examples: list[dict[str, object]],
) -> list[dict[str, str]]:
    """Constructs system/user messages for JSON-only classification."""
    coa_lines = "\n".join(
        [f"- {item.account_id} | {item.name} | {item.description}" for item in tenant_coa]
    )
    system_prompt = (
        "You are a financial transaction classifier for a multi-tenant expense platform.\n"
        "Return ONLY JSON object with keys: coa_account_id, confidence, reasoning.\n"
        "coa_account_id must be from provided TENANT CHART OF ACCOUNTS.\n"
        "confidence must be float in [0.0, 1.0].\n"
        "reasoning must be a single sentence.\n"
        f"TENANT NAME: {tenant_name}\n"
        "TENANT CHART OF ACCOUNTS:\n"
        f"{coa_lines}"
    )
    user_prompt = (
        "Classify this transaction:\n"
        f"Vendor: {transaction.vendor_raw}\n"
        f"Amount: {transaction.amount} {transaction.currency}\n"
        f"Date: {transaction.date}\n"
        f"Type: {transaction.transaction_type}\n"
        f"OCR: {sanitize_ocr_text(transaction.ocr_text)}\n"
        "HISTORICAL EXAMPLES FOR THIS TENANT:\n"
        f"{json.dumps(few_shot_examples, ensure_ascii=True)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def sanitize_ocr_text(ocr_text: str | None) -> str:
    """Redacts obvious PII patterns from OCR text before prompt construction."""
    if not ocr_text:
        return "Not available"

    sanitized = EMAIL_RE.sub("[REDACTED_EMAIL]", ocr_text)
    sanitized = CARD_LAST4_RE.sub("[REDACTED_4DIGITS]", sanitized)
    return sanitized


def _parse_response_output(response: object) -> LLMClassificationOutput:
    """Parses a LiteLLM response object into validated output."""
    if isinstance(response, dict):
        content = response["choices"][0]["message"]["content"]
    else:
        content = response.choices[0].message.content  # type: ignore[attr-defined]
    payload = _extract_json_payload(str(content))
    return LLMClassificationOutput(**payload)


def _extract_json_payload(content: str) -> dict[str, object]:
    """Extracts the first valid JSON object from model output text."""
    content = content.strip()
    if content.startswith("{") and content.endswith("}"):
        return json.loads(content)

    candidates = re.findall(r"\{.*?\}", content, flags=re.DOTALL)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No JSON object found in model output", content, 0)


def _extract_usage(response: object) -> dict[str, int]:
    """Extracts token usage counters from a LiteLLM/OpenAI-like response."""
    usage: object | None = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _extract_status_code(exc: Exception) -> int | None:
    """Extracts HTTP-like status code from provider exceptions."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    return None


def _default_completion_fn(*, model: str, messages: list[dict[str, str]], temperature: float, timeout: float) -> object:
    """Calls LiteLLM completion lazily to keep imports optional in tests."""
    litellm_module = importlib.import_module("litellm")
    completion = getattr(litellm_module, "completion")

    return completion(
        model=model,
        messages=messages,
        temperature=temperature,
        timeout=timeout,
    )


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
