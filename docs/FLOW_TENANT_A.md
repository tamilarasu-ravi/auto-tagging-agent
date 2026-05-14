# Flow walkthrough: **tenant_a**

This file describes what happens **for tenant `tenant_a` only**: config on disk, which Chart of Accounts (CoA) applies, seeded rules, and how a typical transaction moves through the pipeline.

For generic call order (imports, locks, classifier internals), see [`HOW_IT_WORKS.md`](./HOW_IT_WORKS.md).

---

## 1. Identity and config (`data/tenants.json`)

| Field | Value for `tenant_a` |
|--------|----------------------|
| `tenant_id` | `tenant_a` |
| `tenant_name` | `Tenant A` |
| `api_key` | `demo_key_tenant_a` (send as header `X-API-Key`) |
| `review_threshold` | `0.5` |
| `auto_post_threshold` | `0.85` |
| `cold_start` | **not set** → defaults to `false` |
| CoA file | `data/coa/tenant_a.json` |
| Rules file | `data/rules/tenant_a_rules.json` |

### Effective routing thresholds

From `TaggingService._resolve_tenant_routing_thresholds`:

- **Review band:** \( \texttt{confidence} \in [0.5,\,0.85) \) → **`REVIEW_QUEUE`**
- **Auto-post:** `confidence >= 0.85` → **`AUTO_TAG`** (after validation)
- Below `0.5` → **`UNKNOWN`**

Because **`cold_start` is false**, the effective auto-post bar stays **`0.85`** (not tightened).

---

## 2. Chart of accounts (allowed accounts)

Loaded from **`data/coa/tenant_a.json`**. Valid `coa_account_id` values:

| Account ID | Name |
|------------|------|
| `6100` | SaaS Tools |
| `6200` | Cloud & Hosting |
| `7200` | Local Transport |

Any classifier suggestion **not** in this set becomes **`UNKNOWN`** after validation—even if the model “sounds confident”.

---

## 3. Seeded deterministic rules (`data/rules/tenant_a_rules.json`)

Startup merges **seed rules** + **runtime-promoted rules** (`data/runtime/rules/tenant_a.json` if present).

**Seed rule (exact vendor key match after normalization):**

| `vendor_key` (normalized) | `coa_account_id` |
|---------------------------|------------------|
| `zoom us` | `6100` |

Normalization (`normalize_vendor`): lowercasing, punctuation → spaces, collapse spaces. Examples:

- Raw `"Zoom US"` → **`zoom us`** → **matches rule** → **`AUTO_TAG`**, **`source=rule`**, **`confidence=1.0`** (no LLM).

---

## 4. End-to-end flow for tenant_a

### Step A — HTTP request

`POST /transactions/tag` with body including `"tenant_id": "tenant_a"` and header **`X-API-Key: demo_key_tenant_a`**.

If the key or tenant is wrong, `app/main.py` returns **403** / **404** before tagging.

### Step B — Same as all tenants

Inside `TaggingService.tag_transaction` (conceptually):

1. Tenant exists (**yes**).
2. **Idempotency:** `(tenant_a, idempotency_key)` + payload fingerprint.
3. **`vendor_key` = normalize(`vendor_raw`)**.
4. **Rule lookup** `RuleStore.match("tenant_a", vendor_key)`:
   - **`zoom us`** → hit → **AUTO_TAG**, audit + mock accounting sync → **done** (classifier skipped).
5. No rule → **classifier path** using **tenant_a’s CoA** and **few-shot** examples from **tenant_a’s** confirmed-example store (`ConfirmedExampleStore` in SQLite).

### Step C — After classification (when no rule)

- Output validated against **{6100, 6200, 7200}** only.
- **`route_by_confidence`** uses **review 0.5** and **auto-post 0.85** (not 0.95).

So for tenant_a it is **easier** to reach **`AUTO_TAG`** from the LLM path than for tenant_b in cold-start mode: same numeric threshold in JSON, but tenant_b overrides auto-post upward when `cold_start: true`.

### Step D — Human review and learning (still tenant-scoped)

- **`GET /review-queue/tenant_a`** lists pending items for **tenant_a** only.
- **`POST /review-queue/{tx_id}/resolve`** body must include `"tenant_id": "tenant_a"` and the same **API key** for tenant_a.
- On **`correct`**, a **`VendorRule`** can be **`upsert_rule`** → persisted under **`data/runtime/rules/tenant_a.json`** (merged with seeds on reload). **`accept`** confirms without necessarily creating a new vendor rule (`rule_created` stays false), but confirmed examples may still update.

---

## 5. Concrete scenarios (mental pictures)

| You send | What usually happens |
|----------|---------------------|
| `vendor_raw`: `"Zoom US"`, tenant `tenant_a` | Rule match → **`AUTO_TAG`**, account **`6100`**, no LLM. |
| New vendor never seen before | No rule → classifier → validate against CoA → route by confidence (0.5 / 0.85). |
| Same `idempotency_key`, same payload | Cached **`TaggingResult`** (replay-safe). |
| Same `idempotency_key`, different payload | **409** conflict. |

---

## 6. Relation to `scripts/demo_scenario.py`

The demo uses **`tenant_a`** and **`demo_key_tenant_a`**. Transaction **101** intentionally uses **Zoom** to show the **seed rule path**; transactions **102–103** show **review**, **rule promotion**, and a **repeat vendor** hitting the rule.

---

## 7. Isolation reminder

Stores (audit, idempotency, review queue, confirmed examples) are keyed by **`tenant_id`**. **tenant_a** data does not mix with **tenant_b** at the repository layer—as long as API keys and `tenant_id` in the body path match.
