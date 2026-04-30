from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Callable

from app.models import CoAAccount, LLMClassificationOutput, Transaction
from app.pipeline.preprocessor import sanitize_ocr_text

logger = logging.getLogger(__name__)


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
            provider_chain
            if provider_chain is not None
            else _build_provider_chain_from_env()
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
        messages = _build_messages(
            transaction, tenant_coa, tenant_name, few_shot_examples or []
        )
        completion_fn = self._completion_fn or _default_completion_fn

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
        chain.append(
            ProviderConfig(
                name="gemini",
                model=os.getenv("GEMINI_MODEL", "gemini/gemini-2.0-flash"),
            )
        )
    if os.getenv("CLAUDE_API_KEY"):
        chain.append(
            ProviderConfig(
                name="claude",
                model=os.getenv("CLAUDE_MODEL", "anthropic/claude-3-5-sonnet-latest"),
            )
        )
    if os.getenv("OPENAI_API_KEY"):
        chain.append(
            ProviderConfig(
                name="openai", model=os.getenv("OPENAI_MODEL", "openai/gpt-4o")
            )
        )
    return chain


def _build_messages(
    transaction: Transaction,
    tenant_coa: list[CoAAccount],
    tenant_name: str,
    few_shot_examples: list[dict[str, object]],
) -> list[dict[str, str]]:
    """Constructs system/user messages for JSON-only classification."""
    coa_lines = "\n".join(
        [
            f"- {item.account_id} | {item.name} | {item.description}"
            for item in tenant_coa
        ]
    )

    system_prompt = (
        "You are a financial transaction classifier for a multi-tenant expense platform.\n\n"
        "CRITICAL INSTRUCTIONS:\n"
        "1. You must output ONLY a valid, raw JSON object. Do NOT wrap the response in ```json markdown blocks. No preamble, no postscript.\n"
        '2. The JSON must exactly match this schema: {"reasoning": string, "coa_account_id": string | null, "confidence": float}\n'
        "3. 'reasoning' must be generated FIRST. Keep it to ONE sentence (max 50 words). Use this to explain your logic based on the vendor, amount, and CoA definitions.\n"
        "4. 'coa_account_id' MUST be selected from the TENANT CHART OF ACCOUNTS below. If no account matches, or if the vendor is entirely unknown, return null.\n"
        "5. 'confidence' must be a float between 0.0 and 1.0. If you are guessing or the vendor is ambiguous, confidence MUST be below 0.5.A confident, unambiguous match should be 0.85 or above.\n\n"
        "6. If multiple CoA accounts are plausible matches, choose the MOST SPECIFIC one (e.g. prefer 'Cloud Infrastructure' over 'General Expenses' for an AWS charge). If two accounts remain equally specific after reasoning, pick the lower-risk account and set confidence below 0.6 to signal the ambiguity.\n\n"
        f"TENANT NAME: {tenant_name}\n"
        "TENANT CHART OF ACCOUNTS:\n"
        f"{coa_lines}"
    )

    user_prompt = (
        "HISTORICAL EXAMPLES FOR THIS TENANT:\n"
        f"{json.dumps(few_shot_examples, indent=2)}\n\n"
        "-------------------\n"
        "Now, classify THIS target transaction:\n"
        f"Vendor: {transaction.vendor_raw}\n"
        f"Amount: {transaction.amount} {transaction.currency}\n"
        f"Date: {transaction.date}\n"
        f"Type: {transaction.transaction_type}\n"
        f"OCR: {sanitize_ocr_text(transaction.ocr_text)}\n\n"
        'Remember: Return ONLY a raw JSON object starting with {"reasoning": ...'
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


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
    usage: object | None = (
        response.get("usage")
        if isinstance(response, dict)
        else getattr(response, "usage", None)
    )
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


def _default_completion_fn(
    *, model: str, messages: list[dict[str, str]], temperature: float, timeout: float
) -> object:
    """Calls LiteLLM completion lazily to keep imports optional in tests."""
    litellm_module = importlib.import_module("litellm")
    completion = getattr(litellm_module, "completion")

    return completion(
        model=model,
        messages=messages,
        temperature=temperature,
        timeout=timeout,
    )


def _confidence_from_keyword_score(score: float) -> float:
    """Maps a heuristic keyword score to a bounded confidence value.

    Args:
        score: Non-negative heuristic match strength.

    Returns:
        Confidence in [0.25, 0.93] suitable for deterministic routing tests.
    """
    if score >= 20.0:
        return 0.93
    if score >= 15.0:
        return 0.91
    if score >= 10.0:
        return 0.88
    if score >= 7.0:
        return 0.75
    if score >= 5.0:
        return 0.68
    if score >= 3.0:
        return 0.45
    if score > 0.0:
        return 0.32
    return 0.25


def _is_cloud_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like cloud/hosting infrastructure."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("cloud", "hosting", "infrastructure", "cdn", "compute")
    )


def _is_software_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like software/SaaS spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("software", "saas", "subscription", "license", "licenses")
    )


def _is_local_transport_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like local transport/ride-hailing spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text for token in ("transport", "taxi", "ride", "rideshare", "hailing")
    )


def _is_travel_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like broader travel/accommodation spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("travel", "accommodation", "hotel", "flight", "airline")
    )


def _is_professional_services_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like consulting/legal/professional services spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("professional", "consult", "contractor", "legal", "services")
    )


def _score_tenant_coa_candidates(
    vendor_lower: str, tenant_coa: list[CoAAccount]
) -> dict[str, float]:
    """Scores CoA candidates using vendor-family keywords and CoA semantic matching.

    Args:
        vendor_lower: Lowercased vendor string.
        tenant_coa: Tenant CoA accounts available for selection.

    Returns:
        Mapping of account_id -> non-negative score.
    """
    scores: dict[str, float] = {account.account_id: 0.0 for account in tenant_coa}
    cloud_keywords = (
        "aws",
        "amazon web services",
        "gcp",
        "google cloud",
        "azure",
        "cloudflare",
        "cdn",
        "hosting",
        "marketplace",
    )
    software_keywords = (
        "zoom",
        "slack",
        "notion",
        "figma",
        "github",
        "gitlab",
        "atlassian",
        "jira",
        "saas",
        "subscription",
    )
    ride_keywords = (
        "grab",
        "uber",
        "lyft",
        "taxi",
        "bolt",
        "gojek",
        "ride",
        "rideshare",
    )
    travel_keywords = (
        "hotel",
        "flight",
        "airline",
        "airbnb",
        "booking.com",
        "travel",
        "accommodation",
        "agent",
    )
    professional_keywords = (
        "consult",
        "contractor",
        "legal",
        "law firm",
        "attorney",
        "professional services",
    )

    has_cloud_account = any(_is_cloud_account(account) for account in tenant_coa)
    has_local_transport_account = any(
        _is_local_transport_account(account) for account in tenant_coa
    )

    for account in tenant_coa:
        account_id = account.account_id
        if _is_cloud_account(account) and any(
            keyword in vendor_lower for keyword in cloud_keywords
        ):
            scores[account_id] += 15.0
        if _is_software_account(account):
            if any(keyword in vendor_lower for keyword in software_keywords):
                scores[account_id] += 15.0
            # If tenant CoA has no explicit cloud bucket, route cloud vendors to software/cogs-software.
            if (not has_cloud_account) and any(
                keyword in vendor_lower for keyword in cloud_keywords
            ):
                scores[account_id] += 15.0
        if _is_local_transport_account(account) and any(
            keyword in vendor_lower for keyword in ride_keywords
        ):
            scores[account_id] += 8.0
        # If no local transport bucket exists, map ride-hailing to travel/accommodation.
        if _is_travel_account(account):
            if any(keyword in vendor_lower for keyword in travel_keywords):
                scores[account_id] += 6.0
            if (not has_local_transport_account) and any(
                keyword in vendor_lower for keyword in ride_keywords
            ):
                scores[account_id] += 8.0
        if _is_professional_services_account(account) and any(
            keyword in vendor_lower for keyword in professional_keywords
        ):
            scores[account_id] += 8.0

    return scores


def _pick_best_account_id(scores: dict[str, float]) -> tuple[str | None, float]:
    """Selects the highest scoring account id with deterministic tie-breaking.

    Args:
        scores: account_id -> score mapping.

    Returns:
        (best_account_id, best_score) where account id is None if all scores are zero.
    """
    if not scores:
        return None, 0.0

    sorted_candidates = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_account_id, best_score = sorted_candidates[0]

    if best_score <= 0.0:
        return None, 0.0
    return best_account_id, best_score


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
    if "pttep" in vendor_lower:
        sorted_ids = sorted({account.account_id for account in tenant_coa})
        return LLMClassificationOutput(
            coa_account_id=sorted_ids[0],
            confidence=0.31,
            reasoning="Vendor appears fuel/energy-adjacent and should be routed conservatively.",
        )

    if not tenant_coa:
        return LLMClassificationOutput(
            coa_account_id="",
            confidence=0.0,
            reasoning="Tenant chart of accounts is empty; cannot classify deterministically.",
        )

    scores = _score_tenant_coa_candidates(vendor_lower, tenant_coa)
    best_account_id, best_score = _pick_best_account_id(scores)
    if best_account_id is None:
        fallback_id = sorted({account.account_id for account in tenant_coa})[0]
        return LLMClassificationOutput(
            coa_account_id=fallback_id,
            confidence=0.25,
            reasoning="Insufficient deterministic signal; picked lowest account id as conservative placeholder.",
        )

    account_by_id = {account.account_id: account for account in tenant_coa}
    chosen = account_by_id[best_account_id]
    confidence = _confidence_from_keyword_score(best_score)
    reasoning = f"Heuristic keyword match suggests '{chosen.name}' ({chosen.account_id}) for this vendor."
    return LLMClassificationOutput(
        coa_account_id=best_account_id,
        confidence=confidence,
        reasoning=reasoning,
    )
