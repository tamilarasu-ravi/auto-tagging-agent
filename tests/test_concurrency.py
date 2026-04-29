from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def test_parallel_same_idempotency_key_is_processed_once() -> None:
    """Verifies parallel retries with the same idempotency key are deduplicated."""
    tenant_id = "tenant_a"
    tx_suffix = uuid4().hex
    payload = {
        "tx_id": f"tx_parallel_{tx_suffix}",
        "tenant_id": tenant_id,
        "vendor_raw": "Unknown Vendor LLC",
        "amount": "49.00",
        "currency": "USD",
        "date": "2026-04-30",
        "transaction_type": "card",
        "ocr_text": None,
        "idempotency_key": f"idem_parallel_{tx_suffix}",
    }

    with TestClient(app) as client:
        before_count = len(client.get(f"/audit-log/{tenant_id}").json())

    barrier = threading.Barrier(8)

    def send_request() -> tuple[int, dict[str, object]]:
        """Sends one request after all workers reach the same starting point."""
        barrier.wait()
        with TestClient(app) as local_client:
            response = local_client.post("/transactions/tag", json=payload)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: send_request(), range(8)))

    status_codes = [item[0] for item in responses]
    bodies = [item[1] for item in responses]
    first_body = bodies[0]

    assert status_codes == [200] * 8
    assert all(body == first_body for body in bodies)
    assert first_body["status"] == "UNKNOWN"

    with TestClient(app) as client:
        after_count = len(client.get(f"/audit-log/{tenant_id}").json())

    assert after_count == before_count + 1
