# How this project runs (call order, parameters, and chains)

This document describes **what gets called first**, **with what inputs**, and **how control flows** through the MVP. It mirrors the current code under `app/` and `scripts/`.

---

## 1. Entry points (how execution starts)

### 1.A Real HTTP server

```bash
uvicorn app.main:app --reload
```

- **What loads:** Python imports `app.main` and runs **module-level code** in `app/main.py` (see §2).
- **What you call:** Browser or client hits FastAPI routes on that process (e.g. `POST http://127.0.0.1:8000/transactions/tag`).

### 1.B Demo script (in-process app, no separate server)

```bash
python scripts/demo_scenario.py
```

- **What loads:** The script adds the repo root to `sys.path`, then `from app.main import app`.
- **That import runs the same `app/main.py` module setup** as §1.A.
- **What calls the app:** `fastapi.testclient.TestClient(app)` issues fake HTTP requests (`client.post(...)`) against the same routes.

So: **whether you use uvicorn or the demo script, the first “application” code that runs is still `app/main.py` at import time.**

---

## 2. What runs when `app.main` is imported (startup chain)

File: `app/main.py`

Rough order:

1. **Paths**
   - `APP_ROOT` = parent of `app/`
   - `CONFIG_PATH` = `APP_ROOT / "data/tenants.json"`
   - `STATE_DB_PATH` = `APP_ROOT / "data/runtime/state.db"`

2. **`app_config = load_app_config(CONFIG_PATH)`**
   - Reads `data/tenants.json` (tenant list: `tenant_id`, `api_key`, thresholds, `coa_path`, `rules_path`, optional `cold_start`).

3. **SQLite stores** (all use the same `STATE_DB_PATH` unless tests override):
   - `AuditLogStore(STATE_DB_PATH)`
   - `IdempotencyStore(STATE_DB_PATH)`
   - `ReviewQueueStore(STATE_DB_PATH)`
   - `ConfirmedExampleStore(STATE_DB_PATH)`

4. **Per-tenant CoA and API keys**
   - For each tenant in config, read JSON at `tenant_cfg.coa_path`, parse into `list[CoAAccount]`.
   - Build `coa_ids_by_tenant[tenant_id]` = set of allowed `account_id` strings.

5. **`rule_store = RuleStore(APP_ROOT, rules_paths, coa_ids_by_tenant)`**
   - Loads seed rules from each tenant’s `rules_path` JSON; runtime may append promoted rules.

6. **`llm_classifier = LLMClassifier()`**
   - Builds provider chain from environment (see §5). If empty → no live LLM calls; classifier uses deterministic fallback.

7. **`tagging_service = TaggingService(...)`**
   - Injects config, CoA maps, stores, `MockAccountingSyncAdapter`, `processing_lock` (`threading.RLock`), etc.

8. **`app = FastAPI(...)`** + route decorators register handlers.

**Nothing here “tags” yet** until an HTTP handler runs.

---

## 3. HTTP layer: routes, headers, bodies

Still `app/main.py`.

| Route | Method | Headers | Body / params | Delegates to |
|-------|--------|---------|---------------|---------------|
| `/health` | GET | none | — | Inline: `{"status": "ok", ...}` |
| `/transactions/tag` | POST | `X-API-Key` | JSON `Transaction` | `_authorize_tenant_request` → `tagging_service.tag_transaction(transaction)` |
| `/review-queue/{tenant_id}` | GET | `X-API-Key` | path: `tenant_id` | `_authorize_tenant_request` → `review_queue_store.list_by_tenant` |
| `/review-queue/{tx_id}/resolve` | POST | `X-API-Key` | path: `tx_id`; JSON `ReviewResolveRequest` | `_authorize_tenant_request` → `tagging_service.resolve_review_item(tx_id, request)` |
| `/audit-log/{tenant_id}` | GET | `X-API-Key` | path: `tenant_id` | `_authorize_tenant_request` → `audit_store.list_by_tenant` |
| `/rules/{tenant_id}` | GET | `X-API-Key` | path: `tenant_id` | `_authorize_tenant_request` → `rule_store.list_rules` |

### 3.A Auth helper

`_authorize_tenant_request(tenant_id, api_key)`:

- If `tenant_id` not in configured tenants → **404** `Unknown tenant_id.`
- If header missing/wrong vs `api_keys_by_tenant[tenant_id]` → **403** `Invalid API key for tenant.`

---

## 4. Core chain: `TaggingService.tag_transaction`

File: `app/services/tagging_service.py`  
Method: `tag_transaction(self, transaction: Transaction) → TaggingResult`

### 4.A Parameters (what `Transaction` carries)

Important fields used in the pipeline (see `app/models.py` for the full schema):

- `tenant_id`, `tx_id`
- `vendor_raw` → normalized to `vendor_key` via `normalize_vendor`
- Optional `ocr_text` → sanitized for prompts via `sanitize_free_text` where needed
- `idempotency_key` — scoped with `tenant_id` for deduplication

### 4.B Call chain (inside `processing_lock`)

1. **`_ensure_tenant_exists(transaction.tenant_id)`**  
   - Unknown tenant → **404** (HTTPException).

2. **Idempotency**  
   - `payload_fingerprint = SHA256(sorted JSON of full transaction)`.  
   - `idempotency_store.get(tenant_id, idempotency_key)`  
     - Hit + same fingerprint → return **cached** `TaggingResult`.  
     - Hit + different fingerprint → **409** conflict.  
   - On fresh request, after computing `result`, `idempotency_store.put(...)` stores fingerprint + result.

3. **`vendor_key = normalize_vendor(transaction.vendor_raw)`**  
   - Empty vendor edge case skips rule match.

4. **Rule path (deterministic)**  
   - `rule = rule_store.match(tenant_id, vendor_key)` if `vendor_key` non-empty.  
   - **If rule hit:**  
     - Build `TaggingResult(status=AUTO_TAG, source=rule, confidence=1.0, ...)`.  
     - `audit_store.append(result)`  
     - `accounting_sync.sync(result)` (mock adapter)  
     - Skip LLM entirely.  
   - **Else:** classifier path below.

5. **Classifier path (no rule)**

   - Load `tenant_coa`, `tenant_config`, compute thresholds:  
     - `_resolve_tenant_routing_thresholds(tenant_config)`  
     - If `tenant_cfg.cold_start` is true → effective `auto_post_threshold` tightens (e.g. 0.95 in code).

   - **Few-shot examples**  
     - `confirmed_example_store.sample_examples(tenant_id, exclude_vendor_key=vendor_key, tx_id=transaction.tx_id, limit=5)`  
     - Deterministic seeding from `tx_id` keeps demos/tests stable.

   - **`classification_result = llm_classifier.classify(...)`**  
     - Args: `transaction`, `tenant_coa`, `tenant_name`, `few_shot_examples`, `(internal timeout_budget_s)`.  
     - Returns `LLMClassificationResult`: `output` or `None`, plus `provider_name`, token usage fields, `error_reason`, etc.  
     - See §5 for internals.

   - **If `classification_result.output is None`**  
     - `TaggingResult(status=UNKNOWN, source=unknown, confidence=0.0, reasoning includes error_reason, ...)`.  
     - Audit append; **no** mock sync unless your business logic adds it elsewhere (AUTO_TAG only triggers sync elsewhere).

   - **Else validate CoA membership**  
     - `validate_classification_output(classification, valid_coa_ids)`  
     - Invalid → UNKNOWN + audit (“outside tenant CoA”).

   - **Else route by confidence**  
     - `status = route_by_confidence(confidence, review_threshold=..., auto_post_threshold=...)`  
     - Implemented in `app/pipeline/router.py`:  
       - `confidence >= auto_post_threshold` → `AUTO_TAG`  
       - `confidence >= review_threshold` (and below auto_post) → `REVIEW_QUEUE`  
       - else → `UNKNOWN`

   - **Build `TaggingResult`**  
     - For UNKNOWN from low confidence, `coa_account_id` / `confidence` may be cleared per service logic.  
     - `reasoning` passed through `sanitize_free_text`.

   - **Side effects by status**  
     - Always: `audit_store.append(result)` (for this branch).  
     - `AUTO_TAG`: `accounting_sync.sync(result)`.  
     - `REVIEW_QUEUE`: `review_queue_store.add(ReviewQueueItem(...))`.  
     - `UNKNOWN`: no queue, no sync.

6. **`idempotency_store.put(tenant_id, idempotency_key, fingerprint, result)`**  
7. **Return `TaggingResult`.**

---

## 5. Classifier chain: `LLMClassifier.classify`

File: `app/pipeline/llm_classifier.py`

### 5.A Entry

`classify(transaction, tenant_coa, tenant_name, few_shot_examples, timeout_budget_s=15.0)`

### 5.B Provider chain vs fallback

1. **`self._provider_chain`**  
   - Default: `build_provider_chain_from_env()` in `llm_provider.py` (depends on env keys and `LLM_ENABLE_LIVE_CALLS` / test overrides).

2. **If `_provider_chain` is empty**  
   - Immediately returns `LLMClassificationResult(output=classify_transaction_no_llm(transaction, tenant_coa), provider_name="deterministic_fallback", ...)`.

3. **If non-empty**  
   - Builds chat messages: `build_classification_messages(transaction, tenant_coa, tenant_name, few_shot_examples)`.  
   - Iterates providers with bounded time (`deadline = now + timeout_budget_s`), retries per provider policy until success, deadline exceeded, terminal 4xx, etc.  
   - On exhaustion / failure modes → `output=None` with `error_reason`.

So the “chain” is: **deterministic shortcut if no providers** → else **Gemini / Claude / OpenAI-style chain as configured**.

---

## 6. Review resolution chain: `TaggingService.resolve_review_item`

File: `app/services/tagging_service.py`  
Method: `resolve_review_item(tx_id: str, request: ReviewResolveRequest)`

### 6.A Preconditions

- `request.tenant_id` must exist.  
- `request.final_coa_account_id` must be in tenant CoA set → else **422**.  
- Header auth already ensured `tenant_id` matches API key.

### 6.B Under `processing_lock`

1. **Idempotent replay**  
   - If `review_queue_store.get_resolution(tenant_id, tx_id)` exists:  
     - Compare with incoming `action` + `final_coa_account_id` via `_validate_resolution_replay_payload`  
     - Mismatch → **409**  
     - Match → return stored `ReviewResolveResponse`.

2. **`review_queue_store.resolve(tenant_id, tx_id)`**  
   - If no queued item → **404**.

3. **Build resolved `TaggingResult`** (`status=AUTO_TAG`, `source="llm"`, carries reviewer’s final account, etc.)

4. **`audit_store.append(resolved_result)`**  
5. **`accounting_sync.sync(resolved_result)`**  

6. **`confirmed_example_store.add_example(...)`**  
   - Feeds future few-shot sampling.

7. **Rule promotion**  
   - If `request.action == "correct"`:  
     - `VendorRule(...)` → `rule_store.upsert_rule(promoted_rule)`  
     - `rule_created = True`  
   - If `accept`: no new rule (`rule_created = False`) — still persists confirmed example metadata as implemented.

8. **`review_queue_store.save_resolution(...)`** with `ReviewResolveResponse`.  
9. **Return response.**

---

## 7. Diagram (mental model)

```
Client / scripts/demo_scenario.TestClient
    → FastAPI route (main.py): auth header
        → TaggingService.tag_transaction(Transaction)
            → IdempotencyStore (get/put)
            → normalize_vendor
            → RuleStore.match ──hit──► Audit + MockAccountingSync (AUTO_TAG rule)
                         └──miss──► LLMClassifier.classify
                                       → validate_classification_output
                                       → route_by_confidence
                                       → Audit; optional Sync / ReviewQueue / UNKNOWN
        → TaggingService.resolve_review_item(tx_id, ReviewResolveRequest)
            → ReviewQueueStore + RuleStore upsert + ConfirmedExampleStore + Audit + Sync
```

---

## 8. Files to read in order

1. `app/main.py` — wiring, globals, routes  
2. `app/services/tagging_service.py` — business orchestration  
3. `app/pipeline/preprocessor.py` — vendor normalization, text sanitization  
4. `app/pipeline/llm_classifier.py` → `llm_prompt.py`, `llm_provider.py`, `llm_fallback.py`  
5. `app/pipeline/validator.py`, `router.py`  
6. `app/store/*` — persistence behavior  
7. `scripts/demo_scenario.py` — scripted walk through the HTTP API  

Canonical product/architecture narrative: `README.md`, `ARCHITECTURE.md`.
