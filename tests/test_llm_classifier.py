from __future__ import annotations

from dataclasses import dataclass

from app.models import CoAAccount, Transaction
from app.pipeline.llm_classifier import LLMClassifier, ProviderConfig


@dataclass
class DummyError(Exception):
    status_code: int | None
    message: str = "dummy"

    def __str__(self) -> str:
        return self.message


def _sample_transaction() -> Transaction:
    return Transaction(
        tx_id="tx_llm_001",
        tenant_id="tenant_a",
        vendor_raw="AWS Marketplace",
        amount="120.00",
        currency="USD",
        date="2026-04-30",
        transaction_type="card",
        idempotency_key="idem_llm_001",
    )


def _sample_coa() -> list[CoAAccount]:
    return [
        CoAAccount(account_id="6100", name="SaaS Tools", description="SaaS"),
        CoAAccount(account_id="6200", name="Cloud & Hosting", description="Cloud"),
    ]


def test_llm_classifier_4xx_stops_without_fallback() -> None:
    calls: list[str] = []

    def completion_fn(*, model: str, messages: list[dict[str, str]], temperature: float, timeout: float) -> object:
        _ = messages, temperature, timeout
        calls.append(model)
        raise DummyError(status_code=400, message="bad request")

    classifier = LLMClassifier(
        provider_chain=[
            ProviderConfig(name="gemini", model="gemini/model"),
            ProviderConfig(name="claude", model="claude/model"),
        ],
        completion_fn=completion_fn,
    )
    result = classifier.classify(_sample_transaction(), _sample_coa(), tenant_name="Tenant A")

    assert result.output is None
    assert result.error_reason == "provider_4xx"
    assert calls == ["gemini/model"]


def test_llm_classifier_429_retries_then_fallbacks() -> None:
    calls: list[str] = []
    attempts = {"gemini/model": 0}

    def completion_fn(*, model: str, messages: list[dict[str, str]], temperature: float, timeout: float) -> object:
        _ = messages, temperature, timeout
        calls.append(model)
        if model == "gemini/model":
            attempts["gemini/model"] += 1
            raise DummyError(status_code=429, message="rate limit")
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"coa_account_id":"6200","confidence":0.91,"reasoning":"cloud"}'
                    }
                }
            ]
        }

    classifier = LLMClassifier(
        provider_chain=[
            ProviderConfig(name="gemini", model="gemini/model"),
            ProviderConfig(name="claude", model="claude/model"),
        ],
        completion_fn=completion_fn,
        sleep_fn=lambda _: None,
    )
    result = classifier.classify(_sample_transaction(), _sample_coa(), tenant_name="Tenant A")

    assert result.output is not None
    assert result.provider_name == "claude"
    assert attempts["gemini/model"] == 3
    assert calls == ["gemini/model", "gemini/model", "gemini/model", "claude/model"]


def test_llm_classifier_exhausted_chain_returns_unknown_reason() -> None:
    def completion_fn(*, model: str, messages: list[dict[str, str]], temperature: float, timeout: float) -> object:
        _ = model, messages, temperature, timeout
        raise DummyError(status_code=503, message="service unavailable")

    classifier = LLMClassifier(
        provider_chain=[
            ProviderConfig(name="gemini", model="gemini/model"),
            ProviderConfig(name="claude", model="claude/model"),
        ],
        completion_fn=completion_fn,
    )
    result = classifier.classify(_sample_transaction(), _sample_coa(), tenant_name="Tenant A")

    assert result.output is None
    assert result.error_reason == "providers_exhausted"
