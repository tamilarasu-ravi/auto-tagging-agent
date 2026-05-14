# Flow walkthrough: **tenant_b**

This file describes what happens **for tenant `tenant_b` only**: config on disk, CoA accounts, seeded rules (**none**), and the important **`cold_start`** behavior.

For generic pipelines and file-level call chains, see [`HOW_IT_WORKS.md`](./HOW_IT_WORKS.md). For contrast with seeded rules and default thresholds, see [`FLOW_TENANT_A.md`](./FLOW_TENANT_A.md).

---

## 1. Identity and config (`data/tenants.json`)

| Field | Value for `tenant_b` |
|--------|----------------------|
| `tenant_id` | `tenant_b` |
| `tenant_name` | `Tenant B` |
| `api_key` | `demo_key_tenant_b` (header `X-API-Key`) |
| `review_threshold` | `0.5` |
| `auto_post_threshold` | `0.85` (in JSON) |
| **`cold_start`** | **`true`** ← special |
| CoA file | `data/coa/tenant_b.json` |
| Rules seed file | `data/rules/tenant_b_rules.json` (empty list `[]`) |

### Effective routing thresholds (`TaggingService._resolve_tenant_routing_thresholds`)

When **`cold_start: true`**:

- **`review_threshold`** stays **`0.5`** (from config).
- **`auto_post_threshold` is tightened** to **`0.95`** (hard-coded cold-start tightening in code), **not** the `0.85` printed in JSON.

So compared to tenant_a:

| Status | Confidence condition (both must pass validation) |
|--------|--------------------------------------------------|
| **AUTO_TAG** | `confidence >= 0.95` (tenant_b cold start) |
| **REVIEW_QUEUE** | `0.5 <= confidence < 0.95` |
| **UNKNOWN** | `confidence < 0.5` |

Interpretation: **tenant_b behaves like a new customer** — the system is **more conservative** about LLM **`AUTO_TAG`**: outcomes that would auto-post between **0.85 and 0.94** on tenant_a typically become **`REVIEW_QUEUE`** on tenant_b.

---

## 2. Chart of accounts (different from tenant_a)

Loaded from **`data/coa/tenant_b.json`**. Valid `coa_account_id` values:

| Account ID | Name |
|------------|------|
| `5050` | COGS - Software |
| `7100` | Travel & Accommodation |
| `7300` | Professional Services |

The **same LLM pipeline** runs, but the **prompt and validator** only allow **these** IDs. A suggestion like **`6100`** (tenant_a SaaS Tools) → **invalid for tenant_b** → **`UNKNOWN`** (“outside tenant CoA”).

This shows **per-tenant CoA**: a universal model hint is not enough; **server-side membership checks** enforce the right ledger.

---

## 3. Seeded deterministic rules

**`data/rules/tenant_b_rules.json`** is **`[]`** — **no** built-in vendor → account shortcuts at clone time.

Therefore **every new vendor** (until you add seeds or promote rules after review):

1. **Normalizes `vendor_raw` → `vendor_key`**
2. **`RuleStore.match("tenant_b", vendor_key)`** → **`None`**
3. Goes to **`LLMClassifier`** (or deterministic fallback when no provider chain / tests)

After a reviewer **`correct`** on a queued item, runtime rules accumulate under **`data/runtime/rules/tenant_b.json`** (same mechanism as tenant_a).

---

## 4. End-to-end flow for tenant_b

### Step A — HTTP

`POST /transactions/tag` with `"tenant_id": "tenant_b"` and **`X-API-Key: demo_key_tenant_b`**.

Wrong tenant/key → stopped in `main.py` before **`TaggingService`**.

### Step B — Pipeline (same machinery, tenant-scoped data)

1. Idempotency: **`(tenant_b, idempotency_key)`** — isolated from tenant_a keys.
2. No seed rule hit → classify with **tenant_b CoA**, **tenant_b confirmed examples**, **Tenant B** name in prompts.
3. Validate output ⊆ **`{5050, 7100, 7300}`**.
4. Route with **`auto_post_threshold = 0.95`** → **fewer AUTO_TAGs from LLM** than tenant_a.

### Step C — Observability endpoints

Use **`tenant_b`** everywhere:

- `GET /review-queue/tenant_b`
- `GET /audit-log/tenant_b`
- `GET /rules/tenant_b`

---

## 5. Contrasts with tenant_a (summary table)

| Topic | tenant_a | tenant_b |
|--------|-----------|-----------|
| **API key** | `demo_key_tenant_a` | `demo_key_tenant_b` |
| **Cold start** | No | **Yes → auto-post requires ≥ 0.95** |
| **Seed rules** | e.g. `zoom us` → `6100` | **None** |
| **Valid CoA IDs** | `6100`, `6200`, `7200` | `5050`, `7100`, `7300` |
| **Expectation** | More rule shortcuts from day one; slightly easier LLM auto-post (≥ 0.85) | No shortcuts until rules promoted; LLM rarely auto-tags unless **very** confident |

---

## 6. Trying tenant_b locally

Minimal example sequence (pseudo-body — adjust `tx_id` / `idempotency_key` as you like):

1. **`POST /transactions/tag`** — `tenant_id: tenant_b`, new vendor → expect **review** or **unknown** depending on classifier confidence + validation.
2. If **`REVIEW_QUEUE`**, resolve with **`correct`** **only** final accounts **`5050`**, **`7100`**, or **`7300`** (else **422**).
3. **`POST /transactions/tag`** again with same normalized vendor → if a rule was created, expect **`AUTO_TAG`** with **`source=rule`**.

Use **`pytest`** / **`tests/eval/eval_runner.py --tenant tenant_b`** if you want batch behavior over fixtures; README notes tenant_b skews toward REVIEW / UNKNOWN due to **`cold_start`**.

---

## 7. Isolation reminder

SQLite stores partition by **`tenant_id`**. **tenant_b** queues, audits, rules, examples, and idempotency caches are **never read** using tenant_a’s credentials—enforced by API auth + **`tenant_id`** on requests and service methods.
