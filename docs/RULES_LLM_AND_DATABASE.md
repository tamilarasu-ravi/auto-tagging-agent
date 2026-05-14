# Rules, classifier (LLM) usage, and SQLite

This document explains **how vendor rules are created**, **how they are looked up and applied**, **when the classifier runs** (including live LLM vs deterministic fallback), and **how the SQLite database** is used versus JSON rule files.

Related: [`HOW_IT_WORKS.md`](./HOW_IT_WORKS.md), [`FLOW_TENANT_A.md`](./FLOW_TENANT_A.md), [`FLOW_TENANT_B.md`](./FLOW_TENANT_B.md).

---

## 1. What a “rule” is

A **vendor rule** maps one **normalized vendor string** (`vendor_key`) to a **single Chart of Accounts id** (`coa_account_id`) for a **`tenant_id`**.

Model: `VendorRule` in `app/models.py`:

- `tenant_id`, `vendor_key`, `coa_account_id`
- `created_by`: **`"import"`** (seed/manual file) or **`"reviewer"`** (promoted after human review)
- `created_at`, optional `source_tx_id`

Matching is **exact key lookup**, not fuzzy search (`app/pipeline/rule_engine.py`): `rule_index.get(vendor_key)`.

---

## 2. How rules are **created**

### 2.A Seed / “import” rules (JSON checked into the repo)

- **Where:** Paths from `data/tenants.json` → e.g. `data/rules/tenant_a_rules.json`, `data/rules/tenant_b_rules.json`.
- **When loaded:** Process start, inside `RuleStore.__init__` (`app/store/rule_store.py`).
- **Validation:** Each rule’s `coa_account_id` must exist in that tenant’s CoA (`coa_ids_by_tenant`).
- **`created_by`:** Typically **`"import"`** in JSON (operator-maintained catalogue).

Changing these files requires **restarting the server** for the updated seed list to load (in-memory `_base_rules_by_tenant` is built once at startup).

### 2.B Promoted “reviewer” rules (runtime overlay)

1. Transaction has **no** matching rule → classifier runs → routed to **`REVIEW_QUEUE`** (or sometimes **`UNKNOWN`**).
2. A human calls **`POST /review-queue/{tx_id}/resolve`** with **`action: "correct"`** and **`final_coa_account_id`** in that tenant’s CoA.
3. `TaggingService.resolve_review_item` builds a **`VendorRule`** (`created_by="reviewer"`, `vendor_key` from the queued item’s **`vendor_key`**, which came from **`normalize_vendor(vendor_raw)`** at tagging time).
4. **`rule_store.upsert_rule(promoted_rule)`** (`app/store/rule_store.py`):
   - Validates `coa_account_id` ∈ tenant CoA.
   - Inserts/overwrites **`runtime_index[vendor_key]`** (same key → update).
   - Rebuilds **merged** index: `base ∪ runtime`; **runtime wins** on duplicate `vendor_key`.
   - Writes **`data/runtime/rules/{tenant_id}.json`** (`_persist_runtime_rules`).

**`action: "accept"`** resolves the queue item and writes audit/sync and a **confirmed example**, but **`rule_created` stays false** — no deterministic rule is added unless **`correct`** (see tagging service).

Rules themselves are **persisted as JSON files** under `data/runtime/rules/`. They are **not** stored inside SQLite.

---

## 3. How rules are **referenced** and **used**

### 3.A Normalizing the vendor (`vendor_key`)

- Function: **`normalize_vendor(vendor_raw)`** in `app/pipeline/preprocessor.py`.
- Behaviour: lowercase, replace punctuation runs with spaces, collapse whitespace (**exact string drives the lookup**).

Examples:

- `"Zoom US"` → **`zoom us`**
- Vendor rule file must list that **same key** (`"zoom us"`) after normalization semantics you expect once punctuation is stripped.

### 3.B Lookup chain

Inside **`TaggingService.tag_transaction`**:

```text
vendor_key = normalize_vendor(transaction.vendor_raw)
rule = rule_store.match(tenant_id, vendor_key)  if vendor_key  else None
```

- **If `vendor_key` is empty** after normalization → **no rule lookup**; processing goes straight to the **classifier path** (still subject to validation/routing afterward).

### 3.C When a rule hits

If **`rule` is not None**:

1. **`TaggingResult`** is built with **`status=AUTO_TAG`**, **`source=rule`**, **`confidence=1.0`**.
2. **`AuditLogStore.append(result)`**
3. **`MockAccountingSyncAdapter.sync(result)`**
4. **Classifier is not called** (no LiteLLM / no deterministic fallback classifier for that transaction).

---

## 4. When the **classifier** is used (“LLM path” vs fallback)

### 4.A Condition (rule gate)

The classifier runs **only if** no rule applied:

```text
not rule_hit  →  llm_classifier.classify(...)
```

So: **any** future transaction whose **`vendor_key` matches an existing rule never hits the classifier** for tagging.

### 4.B What `LLMClassifier` actually runs

Implementation: **`app/pipeline/llm_classifier.py`**.

- **If the provider chain is empty** (e.g. no API keys configured, tests forcing off, env such that **`build_provider_chain_from_env()`** returns `[]`):
  - **No outbound LLM HTTP call**.
  - Returns **`deterministic_fallback`** (`classify_transaction_no_llm` in `llm_fallback.py`) — still produces `coa_account_id`, `confidence`, `reasoning`-shaped output for downstream validation.

- **If the provider chain is non-empty** (e.g. Gemini / Claude / OpenAI wired via LiteLLM):
  - Builds messages via **`build_classification_messages`** (CoA list, tenant name, transaction, few-shot slice).
  - Calls providers until success, deadline, or terminal failure.

So **“referenced” can mean**:

1. True **multi-provider LLM** calls when keys + env permit.
2. **Deterministic heuristic classifier** masking as the same boundary when no providers are configured (`provider_name="deterministic_fallback"`).

### 4.C Inputs to classification (distinct from rules)

- **Tenant CoA** drives allowed accounts in prompts and validators.
- **Few-shot snippets** come from **`ConfirmedExampleStore.sample_examples`** (SQLite), seeded by reviewer resolutions—not from the vendor rule dictionary.

Classifier output is **always** validated with **`validate_classification_output`** CoA-membership checks before **`route_by_confidence`**.

---

## 5. Summary decision table

| Situation | Rule lookup | Classifier | Typical outcome edge |
|-----------|-------------|------------|-----------------------|
| `vendor_key` matches merged rule index | ✅ hit | ❌ skipped | `AUTO_TAG`, `source=rule` |
| `vendor_key` empty | ❌ skip | ✅ runs | Depends on classifier + thresholds |
| No rule | ❌ miss | ✅ runs | `AUTO_TAG` / `REVIEW_QUEUE` / `UNKNOWN` |
| Providers disabled / empty chain | ❌ (`N/A`) | ✅ `deterministic_fallback` | Same routing + validation gates |

---

## 6. How **SQLite** is used (`data/runtime/state.db`)

All these stores share the **`STATE_DB_PATH`** configured in **`app/main.py`** (typically **`data/runtime/state.db`**).

**Rules do not live in SQLite** — they live in **`data/rules/*.json`** plus **`data/runtime/rules/{tenant_id}.json`** as described above.

### 6.A Tables and roles

| Store class | SQLite table(s) | Purpose |
|-------------|-----------------|--------|
| **`AuditLogStore`** | `audit_log` (`tenant_id`, `payload_json`) | **Append-only** log of **`TaggingResult`** JSON after decisions. |
| **`IdempotencyStore`** | `idempotency` — **PK `(tenant_id, idempotency_key)`** | Caches **`TaggingResult`** + payload fingerprint so retries are safe; conflict → HTTP **409**. |
| **`ReviewQueueStore`** | `review_queue`; `review_resolution` | Pending **`REVIEW_QUEUE`** items keyed by **`(tenant_id, tx_id)`**; persisted resolve responses for idempotent replay. |
| **`ConfirmedExampleStore`** | `confirmed_example` | Rows keyed logically by **`tenant_id`** (+ `vendor_key` column); used to **sample up to N few-shot examples** for prompts (deterministic RNG seed from **`tx_id`**). |

### 6.B Operational notes

- **Tenant isolation:** Each store filters or keys by **`tenant_id`** where applicable — cross-tenant reads are prevented by queries + explicit tenant arguments in APIs.
- **Concurrency:** Writes are guarded with **`threading.RLock`** in services and stores; **`state.db`** is suitable for single-process MVP, not multi-replica without changing storage.
- **Tests:** Fixtures may pass a **temporary** SQLite path (`data/runtime/test_*.db`) — same schema semantics, separate file.

---

## 7. Quick map: persistence by concern

| Concern | Where it lives |
|--------|----------------|
| Canonical seed rules | `data/rules/{tenant}_rules.json` |
| Learned deterministic rules after review **`correct`** | `data/runtime/rules/{tenant_id}.json` (+ in-memory merge) |
| What was decided when (audit) | SQLite `audit_log` |
| Safe replay of submits | SQLite `idempotency` |
| Work waiting for reviewers | SQLite `review_queue` |
| Idempotent reviewer outcomes | SQLite `review_resolution` |
| Past corrections/acceptances for few-shot enrichment | SQLite `confirmed_example` |
| Tenant config + thresholds | `data/tenants.json` |
| Allowed accounts | `data/coa/{tenant}.json` |

Reading **`GET /rules/{tenant_id}`** returns the **merged in-memory rule list** (`RuleStore.list_rules`), i.e. base + runtime overlay for that process lifetime (plus runtime JSON on disk for promoted rules).
