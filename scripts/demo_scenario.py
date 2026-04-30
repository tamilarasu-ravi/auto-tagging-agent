from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient


def _ensure_project_root_on_path() -> None:
    """Adds repository root to sys.path for direct script execution."""
    project_root = Path(__file__).resolve().parents[1]
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)


_ensure_project_root_on_path()

from app.main import app


def _format_line(result: dict[str, object], vendor_label: str) -> str:
    """Formats one audit-style output line for demo readability."""
    confidence = result.get("confidence")
    conf_text = f"{confidence:.2f}" if isinstance(confidence, (float, int)) else "n/a"
    account = result.get("coa_account_id")
    return (
        f"[Audit] tx={result['tx_id']} vendor={vendor_label} "
        f"source={result['source']} conf={conf_text} -> {result['status']} account={account}"
    )


def run_demo_scenario() -> list[str]:
    """Runs a deterministic end-to-end demonstration and returns rendered lines."""
    run_id = uuid4().hex[:8]
    # Use a vendor string that won't accidentally hit unrelated keyword heuristics (e.g. "demo").
    vendor_repeat = f"grab-sg-review-{run_id}"
    lines: list[str] = []

    with TestClient(app) as client:
        headers = {"X-API-Key": "demo_key_tenant_a"}
        tx101 = {
            "tx_id": f"tx101_{run_id}",
            "tenant_id": "tenant_a",
            "vendor_raw": "Zoom US",
            "amount": "20.50",
            "currency": "USD",
            "date": "2026-04-30",
            "transaction_type": "card",
            "ocr_text": None,
            "idempotency_key": f"idem101_{run_id}",
        }
        res101 = client.post("/transactions/tag", json=tx101, headers=headers).json()
        lines.append(
            "[Action] Deterministic vendor rule matched; auto-posting without LLM call."
        )
        lines.append(_format_line(res101, "zoom-us"))

        tx102 = {
            "tx_id": f"tx102_{run_id}",
            "tenant_id": "tenant_a",
            "vendor_raw": vendor_repeat,
            "amount": "18.50",
            "currency": "SGD",
            "date": "2026-04-30",
            "transaction_type": "card",
            "ocr_text": None,
            "idempotency_key": f"idem102_{run_id}",
        }
        res102 = client.post("/transactions/tag", json=tx102, headers=headers).json()
        conf102 = res102.get("confidence")
        conf_str = f"{float(conf102):.2f}" if isinstance(conf102, (float, int)) else "n/a"
        lines.append(
            f"[Action] Routed to human review (confidence {conf_str}) — not safe to auto-post."
        )
        lines.append(_format_line(res102, vendor_repeat))

        resolve_response = client.post(
            f"/review-queue/{tx102['tx_id']}/resolve",
            json={
                "tenant_id": "tenant_a",
                "action": "correct",
                "final_coa_account_id": "6100",
            },
            headers=headers,
        )
        resolve = resolve_response.json()
        if resolve_response.status_code != 200:
            raise RuntimeError(f"demo resolve failed: {resolve_response.status_code} {resolve}")
        lines.append(
            f"[Audit] tx={tx102['tx_id']} reviewer_override "
            f"final={resolve['result']['coa_account_id']} rule_created={resolve['rule_created']}"
        )
        lines.append(
            "[Action] Reviewer correction promoted to a per-vendor rule for future auto-tag."
        )

        tx103 = {
            "tx_id": f"tx103_{run_id}",
            "tenant_id": "tenant_a",
            "vendor_raw": vendor_repeat,
            "amount": "22.00",
            "currency": "SGD",
            "date": "2026-04-30",
            "transaction_type": "card",
            "ocr_text": None,
            "idempotency_key": f"idem103_{run_id}",
        }
        res103 = client.post("/transactions/tag", json=tx103, headers=headers).json()
        lines.append(
            "[Action] Repeat vendor now hits deterministic rule; LLM bypassed for consistency."
        )
        lines.append(_format_line(res103, vendor_repeat))

        tx104 = {
            "tx_id": f"tx104_{run_id}",
            "tenant_id": "tenant_a",
            "vendor_raw": f"Random Unknown {run_id}",
            "amount": "99.99",
            "currency": "USD",
            "date": "2026-04-30",
            "transaction_type": "card",
            "ocr_text": None,
            "idempotency_key": f"idem104_{run_id}",
        }
        res104 = client.post("/transactions/tag", json=tx104, headers=headers).json()
        lines.append(
            "[Action] Unknown vendor refused auto-post (UNKNOWN) — silent miscoding avoided."
        )
        lines.append(_format_line(res104, "unknown-vendor"))

    return lines


def main() -> None:
    for line in run_demo_scenario():
        print(line)


if __name__ == "__main__":
    main()
