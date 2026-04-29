from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException

from app.adapters.accounting_sync import MockAccountingSyncAdapter
from app.config import AppConfig, load_app_config
from app.models import CoAAccount, TaggingResult, Transaction
from app.pipeline.preprocessor import normalize_vendor
from app.store.audit_log import AuditLogStore
from app.store.idempotency_store import IdempotencyStore
from app.store.rule_store import RuleStore


APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_ROOT / "data" / "tenants.json"
RUNTIME_DIR = APP_ROOT / "data" / "runtime"

app = FastAPI(title="Reap CFO Agent", version="0.1.0")
app_config: AppConfig = load_app_config(CONFIG_PATH)
audit_store = AuditLogStore(RUNTIME_DIR / "audit")
accounting_sync = MockAccountingSyncAdapter()
idempotency_store = IdempotencyStore(RUNTIME_DIR / "idempotency")
processing_lock = threading.RLock()

coa_by_tenant: dict[str, list[CoAAccount]] = {}
coa_ids_by_tenant: dict[str, set[str]] = {}
rules_paths: dict[str, str] = {}
for tenant_id, tenant_cfg in app_config.tenants.items():
    coa_payload = json.loads((APP_ROOT / tenant_cfg.coa_path).read_text(encoding="utf-8"))
    coa_by_tenant[tenant_id] = [CoAAccount(**item) for item in coa_payload]
    coa_ids_by_tenant[tenant_id] = {item.account_id for item in coa_by_tenant[tenant_id]}
    rules_paths[tenant_id] = tenant_cfg.rules_path

rule_store = RuleStore(APP_ROOT, rules_paths, coa_ids_by_tenant)


def _transaction_fingerprint(transaction: Transaction) -> str:
    """Builds a stable hash for idempotency payload conflict detection."""
    payload = json.dumps(transaction.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@app.get("/health")
def health() -> dict[str, str]:
    """Returns a basic service health response."""
    return {"status": "ok", "service": "reap-cfo-agent"}


@app.post("/transactions/tag")
def tag_transaction(transaction: Transaction) -> TaggingResult:
    """Tags one transaction using deterministic rules only (core-no-llm mode)."""
    if transaction.tenant_id not in app_config.tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")

    payload_fingerprint = _transaction_fingerprint(transaction)
    with processing_lock:
        cached = idempotency_store.get(transaction.tenant_id, transaction.idempotency_key)
        if cached:
            cached_fingerprint, cached_result = cached
            if cached_fingerprint != payload_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="idempotency_key already used with a different payload.",
                )
            return cached_result

        vendor_key = normalize_vendor(transaction.vendor_raw)
        rule = rule_store.match(transaction.tenant_id, vendor_key) if vendor_key else None

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
            audit_store.append(result)
            accounting_sync.sync(result)
        else:
            result = TaggingResult(
                tx_id=transaction.tx_id,
                tenant_id=transaction.tenant_id,
                status="UNKNOWN",
                source="unknown",
                coa_account_id=None,
                confidence=None,
                reasoning="No deterministic rule match; LLM classification is disabled in core-no-llm mode.",
                timestamp=datetime.now(timezone.utc),
                idempotency_key=transaction.idempotency_key,
            )
            audit_store.append(result)

        idempotency_store.put(
            transaction.tenant_id,
            transaction.idempotency_key,
            payload_fingerprint,
            result,
        )
        return result


@app.get("/audit-log/{tenant_id}")
def get_audit_log(tenant_id: str) -> list[TaggingResult]:
    """Returns tenant-scoped audit events."""
    if tenant_id not in app_config.tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")
    return audit_store.list_by_tenant(tenant_id)


@app.get("/rules/{tenant_id}")
def get_rules(tenant_id: str) -> list[dict[str, object]]:
    """Returns current deterministic rules for one tenant."""
    if tenant_id not in app_config.tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")
    return [rule.model_dump(mode="json") for rule in rule_store.list_rules(tenant_id)]
