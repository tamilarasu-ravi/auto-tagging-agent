from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import AppConfig, load_app_config
from app.main import app
from app.models import Transaction


def test_health_endpoint_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "reap-cfo-agent"}


def test_tag_endpoint_exists_as_placeholder() -> None:
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
        "idempotency_key": "idem_001",
    }

    response = client.post("/transactions/tag", json=payload)

    assert response.status_code == 501
    assert response.json()["detail"] == "Tagging pipeline not implemented yet."


def test_load_app_config_reads_tenants() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = load_app_config(repo_root / "data" / "tenants.json")

    assert isinstance(config, AppConfig)
    assert "tenant_a" in config.tenants
    assert "tenant_b" in config.tenants
    assert config.tenants["tenant_a"].review_threshold == 0.50
    assert config.tenants["tenant_a"].auto_post_threshold == 0.85


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
