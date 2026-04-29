from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException

from app.config import AppConfig, load_app_config
from app.adapters.accounting_sync import MockAccountingSyncAdapter
from app.models import CoAAccount, TaggingResult, Transaction
from app.pipeline.preprocessor import normalize_vendor
from app.store.audit_log import AuditLogStore
from app.store.rule_store import RuleStore


APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_ROOT / "data" / "tenants.json"

app = FastAPI(title="Reap CFO Agent", version="0.1.0")
app_config: AppConfig = load_app_config(CONFIG_PATH)
audit_store = AuditLogStore()
accounting_sync = MockAccountingSyncAdapter()

coa_by_tenant: dict[str, list[CoAAccount]] = {}
rules_paths: dict[str, str] = {}
for tenant_id, tenant_cfg in app_config.tenants.items():
    coa_payload = json.loads((APP_ROOT / tenant_cfg.coa_path).read_text(encoding="utf-8"))
    coa_by_tenant[tenant_id] = [CoAAccount(**item) for item in coa_payload]
    rules_paths[tenant_id] = tenant_cfg.rules_path

rule_store = RuleStore(APP_ROOT, rules_paths)
idempotency_cache: dict[str, dict[str, tuple[str, TaggingResult]]] = {}


def _transaction_fingerprint(transaction: Transaction) -> str:
    payload = json.dumps(transaction.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "reap-cfo-agent"}


@app.post("/transactions/tag")
def tag_transaction(transaction: Transaction) -> TaggingResult:
    if transaction.tenant_id not in app_config.tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")

    tenant_cache = idempotency_cache.setdefault(transaction.tenant_id, {})
    payload_fingerprint = _transaction_fingerprint(transaction)
    cached = tenant_cache.get(transaction.idempotency_key)
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

    tenant_cache[transaction.idempotency_key] = (payload_fingerprint, result)
    return result


@app.get("/audit-log/{tenant_id}")
def get_audit_log(tenant_id: str) -> list[TaggingResult]:
    if tenant_id not in app_config.tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")
    return audit_store.list_by_tenant(tenant_id)


@app.get("/rules/{tenant_id}")
def get_rules(tenant_id: str) -> list[dict[str, str | None]]:
    if tenant_id not in app_config.tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")
    return [rule.model_dump(mode="json") for rule in rule_store.list_rules(tenant_id)]
