from __future__ import annotations

from dataclasses import dataclass

from app.models import CoAAccount, Transaction
from app.pipeline.llm_classifier import (  # pylint: disable=no-name-in-module
    LLMClassifier,
    ProviderConfig,
    classify_transaction_no_llm,
)
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
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
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
    assert result.total_tokens == 15
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


def test_llm_classifier_parses_json_wrapped_in_extra_text() -> None:
    def completion_fn(*, model: str, messages: list[dict[str, str]], temperature: float, timeout: float) -> object:
        _ = model, messages, temperature, timeout
        return {
            "choices": [
                {
                    "message": {
                        "content": 'Sure. {"coa_account_id":"6200","confidence":0.88,"reasoning":"cloud spend"} Thanks.'
                    }
                }
            ]
        }

    classifier = LLMClassifier(
        provider_chain=[ProviderConfig(name="gemini", model="gemini/model")],
        completion_fn=completion_fn,
    )
    result = classifier.classify(_sample_transaction(), _sample_coa(), tenant_name="Tenant A")

    assert result.output is not None
    assert result.output.coa_account_id == "6200"


def test_llm_classifier_injects_few_shot_examples_into_prompt() -> None:
    captured_messages: list[dict[str, str]] = []

    def completion_fn(*, model: str, messages: list[dict[str, str]], temperature: float, timeout: float) -> object:
        _ = model, temperature, timeout
        captured_messages.extend(messages)
        return {
            "choices": [
                {
                    "message": {
                        "content": '{"coa_account_id":"6200","confidence":0.90,"reasoning":"cloud"}'
                    }
                }
            ]
        }

    classifier = LLMClassifier(
        provider_chain=[ProviderConfig(name="gemini", model="gemini/model")],
        completion_fn=completion_fn,
    )
    examples = [
        {"vendor": "aws-marketplace", "coa_account_id": "6200"},
        {"vendor": "grab-sg-0023", "coa_account_id": "7200"},
    ]
    classifier.classify(
        _sample_transaction(),
        _sample_coa(),
        tenant_name="Tenant A",
        few_shot_examples=examples,
    )

    user_message = next(message for message in captured_messages if message["role"] == "user")
    assert '"vendor": "aws-marketplace"' in user_message["content"]
    assert '"vendor": "grab-sg-0023"' in user_message["content"]


def _tenant_b_coa() -> list[CoAAccount]:
    return [
        CoAAccount(account_id="5050", name="COGS - Software", description="Software costs booked under cost of goods sold"),
        CoAAccount(account_id="7100", name="Travel & Accommodation", description="Flights, hotels, and related travel expenses"),
        CoAAccount(account_id="7300", name="Professional Services", description="Contractors, legal, and consulting fees"),
    ]


def test_classify_transaction_no_llm_maps_aws_to_tenant_b_cogs_account() -> None:
    tx = Transaction(
        tx_id="tx_det_aws_b",
        tenant_id="tenant_b",
        vendor_raw="AWS Marketplace",
        amount="10.00",
        currency="USD",
        date="2026-04-30",
        transaction_type="card",
        idempotency_key="idem_det_aws_b",
    )
    output = classify_transaction_no_llm(tx, _tenant_b_coa())

    assert output.coa_account_id == "5050"
    assert output.confidence >= 0.90


def test_classify_transaction_no_llm_maps_grab_to_tenant_b_travel_account() -> None:
    tx = Transaction(
        tx_id="tx_det_grab_b",
        tenant_id="tenant_b",
        vendor_raw="Grab SG 1234",
        amount="10.00",
        currency="SGD",
        date="2026-04-30",
        transaction_type="card",
        idempotency_key="idem_det_grab_b",
    )
    output = classify_transaction_no_llm(tx, _tenant_b_coa())

    assert output.coa_account_id == "7100"
    assert 0.50 <= output.confidence < 0.85


def test_classify_transaction_no_llm_pttep_is_conservatively_low_confidence() -> None:
    tx = Transaction(
        tx_id="tx_det_pttep",
        tenant_id="tenant_b",
        vendor_raw="PTTEP THAILAND FUEL 0049",
        amount="10.00",
        currency="THB",
        date="2026-04-30",
        transaction_type="card",
        idempotency_key="idem_det_pttep",
    )
    output = classify_transaction_no_llm(tx, _tenant_b_coa())

    assert output.coa_account_id == "5050"
    assert output.confidence == 0.31


def test_classify_transaction_no_llm_uses_coa_semantics_not_hardcoded_ids() -> None:
    tenant_c_coa = [
        CoAAccount(account_id="c-001", name="Cloud Infrastructure", description="Cloud hosting and compute"),
        CoAAccount(account_id="c-002", name="Business Travel", description="Travel and accommodation costs"),
    ]
    aws_tx = Transaction(
        tx_id="tx_det_aws_c",
        tenant_id="tenant_c",
        vendor_raw="AWS Marketplace",
        amount="10.00",
        currency="USD",
        date="2026-04-30",
        transaction_type="card",
        idempotency_key="idem_det_aws_c",
    )
    grab_tx = Transaction(
        tx_id="tx_det_grab_c",
        tenant_id="tenant_c",
        vendor_raw="Grab SG 8899",
        amount="16.00",
        currency="SGD",
        date="2026-04-30",
        transaction_type="card",
        idempotency_key="idem_det_grab_c",
    )

    aws_output = classify_transaction_no_llm(aws_tx, tenant_c_coa)
    grab_output = classify_transaction_no_llm(grab_tx, tenant_c_coa)

    assert aws_output.coa_account_id == "c-001"
    assert aws_output.confidence >= 0.90
    assert grab_output.coa_account_id == "c-002"
    assert 0.50 <= grab_output.confidence < 0.85
