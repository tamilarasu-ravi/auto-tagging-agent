from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import AppConfig, load_app_config
from app.main import app
from app.models import Transaction


def _unique_key(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _headers(tenant_id: str) -> dict[str, str]:
    if tenant_id == "tenant_a":
        return {"X-API-Key": "demo_key_tenant_a"}
    return {"X-API-Key": "demo_key_tenant_b"}


def test_health_endpoint_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "reap-cfo-agent"}


def test_tag_endpoint_rule_match_auto_tags() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "tx_001",
        "tenant_id": "tenant_a",
        "vendor_raw": "Zoom US",
        "amount": "20.50",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_001"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "AUTO_TAG"
    assert data["source"] == "rule"
    assert data["coa_account_id"] == "6100"
    assert data["confidence"] == 1.0


def test_tag_endpoint_no_rule_routes_to_unknown() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "tx_002",
        "tenant_id": "tenant_a",
        "vendor_raw": "Unknown Vendor LLC",
        "amount": "41.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_002"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "UNKNOWN"
    assert data["source"] == "unknown"
    assert data["coa_account_id"] is None


def test_idempotency_returns_cached_result_for_same_payload() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "tx_003",
        "tenant_id": "tenant_a",
        "vendor_raw": "Unknown Vendor LLC",
        "amount": "41.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_003"),
    }

    first = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))
    second = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()


def test_idempotency_key_conflict_when_payload_differs() -> None:
    client = TestClient(app)
    payload_a = {
        "tx_id": "tx_004",
        "tenant_id": "tenant_a",
        "vendor_raw": "Unknown Vendor LLC",
        "amount": "41.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_004"),
    }
    payload_b = {**payload_a, "amount": "42.00"}

    first = client.post("/transactions/tag", json=payload_a, headers=_headers("tenant_a"))
    second = client.post("/transactions/tag", json=payload_b, headers=_headers("tenant_a"))

    assert first.status_code == 200
    assert second.status_code == 409


def test_tag_endpoint_llm_path_auto_tags_when_confidence_is_high() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "tx_005",
        "tenant_id": "tenant_a",
        "vendor_raw": "AWS Marketplace",
        "amount": "210.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_005"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "AUTO_TAG"
    assert data["source"] == "llm"
    assert data["coa_account_id"] == "6200"
    assert data["confidence"] == 0.91


def test_tag_endpoint_llm_path_routes_to_review_queue_when_confidence_is_medium() -> None:
    client = TestClient(app)
    vendor_raw = f"Grab SG {_unique_key('mid')}"
    payload = {
        "tx_id": "tx_006",
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "18.50",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_006"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))
    queue_response = client.get("/review-queue/tenant_a", headers=_headers("tenant_a"))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "REVIEW_QUEUE"
    assert data["source"] == "llm"
    assert data["coa_account_id"] == "7200"
    assert data["confidence"] == 0.75

    assert queue_response.status_code == 200
    review_items = queue_response.json()
    assert any(item["tx_id"] == "tx_006" for item in review_items)


def test_tag_endpoint_tenant_b_cold_start_routes_aws_to_review_queue() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "tx_007",
        "tenant_id": "tenant_b",
        "vendor_raw": "AWS Marketplace",
        "amount": "350.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_007"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_b"))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "REVIEW_QUEUE"
    assert data["source"] == "llm"
    assert data["coa_account_id"] == "5050"
    assert isinstance(data["confidence"], (float, int))


def test_get_rules_endpoint_returns_rules_for_tenant() -> None:
    client = TestClient(app)
    response = client.get("/rules/tenant_a", headers=_headers("tenant_a"))

    assert response.status_code == 200
    rules = response.json()
    assert isinstance(rules, list)
    assert any(rule.get("vendor_key") == "zoom us" for rule in rules)


def test_tenant_isolation_rejects_wrong_api_key_for_path_tenant() -> None:
    client = TestClient(app)
    response = client.get("/review-queue/tenant_b", headers=_headers("tenant_a"))

    assert response.status_code == 403


def test_tag_endpoint_scrubs_ocr_text_in_llm_path() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": _unique_key("tx_ocr"),
        "tenant_id": "tenant_a",
        "vendor_raw": f"OCR Smoke Vendor {_unique_key('ocr')}",
        "amount": "12.34",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": "Contact payer@example.com and ref 4242",
        "idempotency_key": _unique_key("idem_ocr"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    assert response.status_code == 200
    audit = client.get("/audit-log/tenant_a", headers=_headers("tenant_a")).json()
    matching = [event for event in audit if event.get("tx_id") == payload["tx_id"]]
    assert matching
    for event in matching:
        reasoning = event.get("reasoning") or ""
        assert "payer@example.com" not in reasoning
        assert "4242" not in reasoning


def test_resolve_review_item_accept_removes_from_queue() -> None:
    client = TestClient(app)
    tx_id = _unique_key("tx_008")
    vendor_raw = f"Grab SG {_unique_key('accept')}"
    payload = {
        "tx_id": tx_id,
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "18.50",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_008"),
    }
    client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    resolve_response = client.post(
        f"/review-queue/{tx_id}/resolve",
        json={
            "tenant_id": "tenant_a",
            "action": "accept",
            "final_coa_account_id": "7200",
            "reviewer_id": "reviewer_1",
        },
        headers=_headers("tenant_a"),
    )
    queue_response = client.get("/review-queue/tenant_a", headers=_headers("tenant_a"))

    assert resolve_response.status_code == 200
    data = resolve_response.json()
    assert data["result"]["status"] == "AUTO_TAG"
    assert data["result"]["coa_account_id"] == "7200"
    assert data["rule_created"] is False
    assert data["resolved_by"] == "reviewer_1"
    assert data["resolved_at"] is not None
    assert queue_response.status_code == 200
    assert all(item["tx_id"] != tx_id for item in queue_response.json())

    corpus = client.get("/corpus/tenant_a", headers=_headers("tenant_a")).json()
    match = [row for row in corpus if row["tx_id"] == tx_id]
    assert len(match) == 1
    assert match[0]["final_coa_account_id"] == "7200"
    assert match[0]["resolution_action"] == "accept"
    assert match[0]["vendor_raw"] == vendor_raw
    assert match[0]["amount"] == "18.50"
    assert match[0]["currency"] == "SGD"


def test_resolve_review_item_correct_overrides_coa() -> None:
    client = TestClient(app)
    tx_id = _unique_key("tx_009")
    vendor_raw = f"Grab SG {_unique_key('correct')}"
    payload = {
        "tx_id": tx_id,
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "18.50",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_009"),
    }
    client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    resolve_response = client.post(
        f"/review-queue/{tx_id}/resolve",
        json={
            "tenant_id": "tenant_a",
            "action": "correct",
            "final_coa_account_id": "6100",
        },
        headers=_headers("tenant_a"),
    )

    assert resolve_response.status_code == 200
    data = resolve_response.json()
    assert data["result"]["status"] == "AUTO_TAG"
    assert data["result"]["coa_account_id"] == "6100"
    assert data["rule_created"] is True


def test_resolve_review_item_is_idempotent_after_first_resolution() -> None:
    client = TestClient(app)
    tx_id = _unique_key("tx_009b")
    vendor_raw = f"Grab SG {_unique_key('replay')}"
    payload = {
        "tx_id": tx_id,
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "18.50",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_009b"),
    }
    client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    request_payload = {
        "tenant_id": "tenant_a",
        "action": "correct",
        "final_coa_account_id": "6100",
        "reviewer_id": "reviewer_replay",
    }
    first = client.post(
        f"/review-queue/{tx_id}/resolve",
        json=request_payload,
        headers=_headers("tenant_a"),
    )
    second = client.post(
        f"/review-queue/{tx_id}/resolve",
        json=request_payload,
        headers=_headers("tenant_a"),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()

    corpus_rows = [r for r in client.get("/corpus/tenant_a", headers=_headers("tenant_a")).json() if r["tx_id"] == tx_id]
    assert len(corpus_rows) == 1


def test_resolve_review_item_rejects_conflicting_replay_payload() -> None:
    client = TestClient(app)
    tx_id = _unique_key("tx_009c")
    vendor_raw = f"Grab SG {_unique_key('replay_conflict')}"
    payload = {
        "tx_id": tx_id,
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "18.50",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_009c"),
    }
    client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    first = client.post(
        f"/review-queue/{tx_id}/resolve",
        json={
            "tenant_id": "tenant_a",
            "action": "correct",
            "final_coa_account_id": "6100",
            "reviewer_id": "reviewer_conflict",
        },
        headers=_headers("tenant_a"),
    )
    conflicting_second = client.post(
        f"/review-queue/{tx_id}/resolve",
        json={
            "tenant_id": "tenant_a",
            "action": "accept",
            "final_coa_account_id": "7200",
            "reviewer_id": "reviewer_conflict_2",
        },
        headers=_headers("tenant_a"),
    )

    assert first.status_code == 200
    assert conflicting_second.status_code == 409


def test_reviewer_correction_promotes_vendor_rule_for_next_transaction() -> None:
    client = TestClient(app)
    vendor_raw = f"Grab SG {_unique_key('vendor')}"
    first_tx_id = _unique_key("tx_011")
    first_payload = {
        "tx_id": first_tx_id,
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "18.50",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_011"),
    }
    first_response = client.post("/transactions/tag", json=first_payload, headers=_headers("tenant_a"))
    assert first_response.status_code == 200
    assert first_response.json()["status"] == "REVIEW_QUEUE"

    resolve_response = client.post(
        f"/review-queue/{first_tx_id}/resolve",
        json={
            "tenant_id": "tenant_a",
            "action": "correct",
            "final_coa_account_id": "6100",
        },
        headers=_headers("tenant_a"),
    )
    assert resolve_response.status_code == 200
    assert resolve_response.json()["rule_created"] is True

    second_payload = {
        "tx_id": _unique_key("tx_012"),
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "22.00",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_012"),
    }
    second_response = client.post("/transactions/tag", json=second_payload, headers=_headers("tenant_a"))

    assert second_response.status_code == 200
    data = second_response.json()
    assert data["status"] == "AUTO_TAG"
    assert data["source"] == "rule"
    assert data["confidence"] == 1.0
    assert data["coa_account_id"] == "6100"


def test_resolve_review_item_rejects_invalid_tenant_coa_account() -> None:
    client = TestClient(app)
    tx_id = _unique_key("tx_010")
    vendor_raw = f"Grab SG {_unique_key('invalid')}"
    payload = {
        "tx_id": tx_id,
        "tenant_id": "tenant_a",
        "vendor_raw": vendor_raw,
        "amount": "18.50",
        "currency": "SGD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_010"),
    }
    client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    resolve_response = client.post(
        f"/review-queue/{tx_id}/resolve",
        json={
            "tenant_id": "tenant_a",
            "action": "correct",
            "final_coa_account_id": "9999",
        },
        headers=_headers("tenant_a"),
    )

    assert resolve_response.status_code == 422


def test_tenant_auth_rejects_invalid_api_key() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "manual_auth_001",
        "tenant_id": "tenant_a",
        "vendor_raw": "Zoom US",
        "amount": "20.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_auth_001"),
    }

    response = client.post("/transactions/tag", json=payload, headers={"X-API-Key": "wrong_key"})

    assert response.status_code == 403


def test_corpus_endpoint_requires_matching_api_key() -> None:
    """GET /corpus/{tenant_id} uses same tenant-scoped API key policy as other reads."""
    client = TestClient(app)
    r = client.get("/corpus/tenant_b", headers=_headers("tenant_a"))
    assert r.status_code == 403


def test_tag_endpoint_rejects_overlong_vendor_raw() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "manual_vendor_len_001",
        "tenant_id": "tenant_a",
        "vendor_raw": "X" * 501,
        "amount": "20.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": _unique_key("idem_vendor_len_001"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    assert response.status_code == 422


def test_tag_endpoint_rejects_overlong_ocr_text() -> None:
    client = TestClient(app)
    payload = {
        "tx_id": "manual_ocr_len_001",
        "tenant_id": "tenant_a",
        "vendor_raw": "AWS Marketplace",
        "amount": "20.00",
        "currency": "USD",
        "date": "2026-04-29",
        "transaction_type": "card",
        "ocr_text": "x" * 2001,
        "idempotency_key": _unique_key("idem_ocr_len_001"),
    }

    response = client.post("/transactions/tag", json=payload, headers=_headers("tenant_a"))

    assert response.status_code == 422


def test_load_app_config_reads_tenants() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = load_app_config(repo_root / "data" / "tenants.json")

    assert isinstance(config, AppConfig)
    assert "tenant_a" in config.tenants
    assert "tenant_b" in config.tenants
    assert config.tenants["tenant_a"].review_threshold == 0.50
    assert config.tenants["tenant_a"].auto_post_threshold == 0.85
    assert config.tenants["tenant_a"].cold_start is False
    assert config.tenants["tenant_b"].cold_start is True


def test_transaction_model_parses_schema() -> None:
    tx = Transaction(
        tx_id="tx_100",
        tenant_id="tenant_a",
        vendor_raw="AWS Marketplace, Inc.",
        amount=Decimal("1240.00"),
        currency="USD",
        date="2026-04-29",
        transaction_type="card",
        idempotency_key="idem_100",
    )

    assert tx.amount == Decimal("1240.00")
    assert tx.tenant_id == "tenant_a"
