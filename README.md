# AI-CFO — Transaction Auto-Tagging Agent

An MVP prototype for **Transaction auto-tagging and close acceleration**.

The agent auto-tags each transaction to a tenant-specific Chart of Accounts (CoA), enforcing one non-negotiable safety constraint:

> **Silent miscoding is worse than refusal.**
> The system never guesses quietly. Low-confidence cases are routed to review or marked UNKNOWN.

---

## Candidate Info

- **Workflow chosen**: **Workflow 1 — Transaction auto-tagging and close acceleration**
- **Why this workflow**: high operational leverage during month-end close, clear safety boundaries, and measurable business impact (automation rate, review rate, close-time reduction).
- **Implementation intent**: deliver a correctness-first MVP that is runnable end-to-end now and explicit about what must change before production rollout.

## Execution Notes (Time Budget)

- **Estimated hands-on build time**: approximately 6 hours, aligned with the take-home guidance.
- **Delivery approach**: iterative test-first slices (bootstrap -> core loop -> safety hardening -> tests -> docs).
- **Why commit count is high**: commits are intentionally small checkpoints for traceability and rollback safety during rapid iteration, not an indicator of expanded scope.
- **Scope discipline**: production-grade extensions are documented as follow-ups in `ARCHITECTURE.md` and sections 9/12 of this README, rather than implemented in MVP.

## Documentation Map

- `README.md` (this file): executive summary, MVP scope, setup/run/test instructions, and submission context.
- `ARCHITECTURE.md`: canonical system design, invariants, API architecture contracts, failure-mode handling, and MVP-vs-production boundary decisions.

## Quick 5-Min Verify

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
pytest -q
BASE_URL="http://127.0.0.1:8011" AUTO_START_SERVER=true SMOKE_LLM_ENABLE_LIVE_CALLS=false bash scripts/smoke_test.sh
```

## MVP At A Glance (30 seconds)

- Tags transactions to tenant CoA with a safety-first pipeline (`rule -> classifier -> review/unknown`).
- Hard-validates classifier outputs against tenant CoA before any auto-post action.
- Uses confidence routing with cold-start tightening to bias toward human review.
- Persists audit/idempotency/review state in SQLite and supports reviewer-driven rule promotion.
- Enforces request guardrails at API boundary (`vendor_raw` max 500 chars, `ocr_text` max 2000 chars).

## Out of Scope for This MVP

- Tax code and tracking-category prediction with dependency constraints.
- Distributed concurrency guarantees across multi-worker or multi-replica deployments.
- Advanced DLP/NER for all PII classes (current redaction is regex-focused).
- Retrieval-based few-shot selection (current approach samples tenant-confirmed examples).

**Why retrieval is deferred in this MVP**: the take-home is intentionally time-boxed and safety-critical. This implementation prioritizes deterministic correctness gates (tenant CoA validation, confidence routing, idempotency, auditability) before introducing a retrieval layer. Retrieval is planned as the first quality upgrade once the baseline safety loop is validated.

## Production Migration Plan (Current -> Target)

| Area               | Current MVP                      | Production Target                                    | Migration Trigger                                  |
| :----------------- | :------------------------------- | :--------------------------------------------------- | :------------------------------------------------- |
| Persistence        | SQLite + JSON seed/runtime files | Postgres + migrations + backups                      | Multi-worker deploy or sustained higher throughput |
| Queueing & retries | In-process sync flow             | Queue-backed async workers + DLQ                     | Month-end burst pressure / downstream rate limits  |
| AuthN/AuthZ        | Static tenant API keys           | OAuth2/API gateway/mTLS + key rotation               | External customer access and compliance reviews    |
| LLM operations     | Env-based provider chain         | Budget caps, circuit breakers, tenant policies       | Live tenant rollout with spend/SLA targets         |
| Retrieval quality  | Random tenant few-shot sampling  | Retrieval-based similarity examples (e.g., pgvector) | Long-tail performance plateau in eval metrics      |
| Observability      | Application logs                 | Metrics/traces/alerts (OpenTelemetry)                | On-call ownership and SLO commitments              |

## Known Risks & Mitigations

- **Live provider availability**: upstream model/key issues can produce 4xx/5xx.  
  **Mitigation**: safe refusal path to `UNKNOWN`; fallback + retries for retriable errors.
- **Reviewer trust asymmetry**: a wrong correction can promote a bad deterministic rule.  
  **Mitigation**: rule lineage (`created_by`, `created_at`, `source_tx_id`) and audit trail.
- **Single-process lock scope**: `RLock` is process-local only.  
  **Mitigation**: explicit single-process MVP constraint; production move to DB-backed concurrency control.
- **Confidence calibration drift**: self-reported confidence may deviate over time.  
  **Mitigation**: periodic eval harness run and recalibration trigger threshold.
- **Large-CoA prompt pressure**: CoA prompt truncation/telemetry (`coa_truncated`) is not implemented in MVP.  
  **Mitigation**: keep tenant CoA small for MVP fixtures; add truncation policy + telemetry before production rollout.

---

## Table of Contents

1. [Executive Summary & Product Philosophy](#1-executive-summary--product-philosophy)
2. [System Architecture](#2-system-architecture)
3. [How This Addresses Workflow 1 Challenges](#3-how-this-addresses-workflow-1-challenges)
4. [LLM Prompt Design](#4-llm-prompt-design)
5. [Data Schemas & Contracts](#5-data-schemas--contracts)
6. [Failure Modes & Edge Cases](#6-failure-modes--edge-cases)
7. [Evaluation Strategy](#7-evaluation-strategy)
8. [Architectural & Technology Choices](#8-architectural--technology-choices)
9. [Production-Readiness Considerations](#9-production-readiness-considerations)
10. [How to Run the MVP](#10-how-to-run-the-mvp)
11. [Explicit Assumptions](#11-explicit-assumptions)
12. [Open Questions for Production Scaling (Post-MVP)](#12-open-questions-for-production-scaling-post-mvp)

---

## 1. Executive Summary & Product Philosophy

Finance teams spend significant time during close categorizing transactions against their accounting structure. For Reap's customers (mid-market, multi-currency), this is repetitive work at meaningful volume (~250 transactions per month). Today, that accounts for roughly a week of close work per month.

**This MVP's product philosophy is: correctness first, automation second.**

The agent is built around a **hybrid decision system**:

- **Deterministic rules** — fast, reliable, zero-cost for recurring vendor patterns per tenant.
- **LLM classification** — semantic categorization for novel cases, constrained strictly to the tenant's CoA.
- **Human-in-the-loop (HITL)** — any uncertain decision is routed to review. Zero silent errors is a hard invariant, not a nice-to-have.

**Primary success metric**: Auto-Tagging Rate (% of transactions tagged without human intervention), measured per tenant, trending upward over time as the rule store grows.

**Business KPI targets (post-MVP rollout goals):**

- Reduce average monthly close effort for this workflow from ~7 days toward ~2-3 days.
- Reach 60%+ safe automation (`AUTO_TAG`) within the first 90 days of tenant onboarding.
- Keep review backlog healthy (for example, 95% of review items resolved within 24 hours during month-end windows).

**Scope (MVP)**: CoA tagging only. Tax codes, tracking categories, and other metadata are intentionally deferred so the correctness-first loop stays high-quality and testable within the time budget.

---

## 2. System Architecture

Canonical architecture details now live in `ARCHITECTURE.md`.

This section remains as a reviewer-friendly overview in the README; for implementation boundaries and production-oriented architecture decisions, use `ARCHITECTURE.md` as source of truth.

### 2.1 High-Level Flow

```
[Transaction Event Stream]
        │
        ▼
[1. Pre-processing & Enrichment]
        │   - normalize vendor string (lowercase, collapse whitespace, strip punctuation)
        │   - attach OCR receipt snippet if available
        │   - attach tenant CoA list
        │   - attach tenant vendor→CoA rule store
        ▼
[2. Deterministic Rule Engine]
        │── (rule match found?) ──> AUTO_TAG (source=rule, conf=1.00)
        │                               │
        │                               ▼
        │                        [Mock Accounting Sync] + Audit Log
        ▼ (no match)
[3. PII Stripping]
        │   - strip cardholder name, card last-4, and any personal identifiers from ocr_text
        │   - MVP: regex-based scrubbing of card last-4 patterns and email addresses; named-entity recognition (NER) deferred to production
        │   - only sanitized fields are forwarded to the LLM
        ▼
[4. LLM Classification]  (provider chain: Gemini → Claude → OpenAI, on error/timeout)
        │   - system prompt: role + safety instruction
        │   - injected: tenant CoA list (id + name + description)
        │   - injected: few-shot examples from tenant history
        │   - injected: transaction fields + sanitized OCR snippet
        │   - constrained JSON output: {coa_account_id, confidence, reasoning}
        ▼
[5. Output Validation]
        │   - is JSON schema valid?
        │   - is coa_account_id present in the tenant's CoA?
        │   - is confidence in [0.0, 1.0]?
        │── (any check fails) ──> UNKNOWN + Audit Log
        ▼ (valid output)
[6. Confidence Router]
        │── (conf >= AUTO_POST_THRESHOLD)          ──> AUTO_TAG (source=llm)  ──> [Mock Accounting Sync] + Audit Log
        │── (REVIEW_THRESHOLD <= conf < AUTO_POST) ──> REVIEW_QUEUE           ──> Audit Log
        └── (conf < REVIEW_THRESHOLD)              ──> UNKNOWN                ──> Audit Log
        ▼
[7. Learning Loop]
        - reviewer accepts or corrects suggestion
        - correction → promoted to tenant-specific vendor rule
        - rule store persisted; next identical vendor → deterministic auto-tag
```

### 2.2 Key Invariants

| Invariant                                                                                      | Enforcement point                                                                                                       |
| :--------------------------------------------------------------------------------------------- | :---------------------------------------------------------------------------------------------------------------------- |
| LLM must only suggest accounts from the tenant's CoA                                           | Application-layer validation after LLM response (not just prompt)                                                       |
| Every decision is logged with source, confidence, and reasoning                                | Audit log written before any sync                                                                                       |
| Provider timeouts/5xx/connection errors → fallback chain exhausted → `UNKNOWN`, never auto-tag | `llm_classifier.py` fallback chain; final catch routes to `UNKNOWN`                                                     |
| Provider 4xx (e.g., prompt too long / content policy) → `UNKNOWN`, never auto-tag              | `llm_classifier.py` treats 4xx as non-retriable (no fallback) to avoid leaking the same request to additional providers |
| Invalid schema or hallucinated CoA account → `UNKNOWN`, never auto-tag                         | Output validation layer (Step 5)                                                                                        |
| Thresholds are per-tenant configuration, not hardcoded                                         | `TenantConfig` model                                                                                                    |

### 2.3 Project Structure

```
auto-tagging-agent/
├── ARCHITECTURE.md              # MVP vs production boundary table
├── app/
│   ├── main.py                  # FastAPI wiring (routes delegate to services)
│   ├── models.py                # Pydantic schemas (Transaction, CoAAccount, LLMOutput, etc.)
│   ├── config.py                # Tenant config loader (thresholds, CoA, rules)
│   ├── services/
│   │   └── tagging_service.py   # Tag + resolve orchestration (rule-first, classifier, audit)
│   ├── pipeline/
│   │   ├── preprocessor.py      # Vendor normalization; OCR PII redaction for prompts
│   │   ├── rule_engine.py       # Deterministic vendor→CoA matching
│   │   ├── llm_classifier.py    # Classifier facade (fallback chain + deterministic path)
│   │   ├── llm_prompt.py        # Chat message construction for LiteLLM
│   │   ├── llm_provider.py      # Provider chain env + HTTP completion + response parse
│   │   ├── llm_fallback.py      # No-LLM deterministic CoA scoring
│   │   ├── llm_types.py         # ProviderConfig / LLMClassificationResult
│   │   ├── validator.py         # Output schema + CoA membership validation
│   │   └── router.py            # Confidence-based routing
│   ├── store/
│   │   ├── audit_log.py         # Append-only audit log (SQLite)
│   │   ├── idempotency_store.py # Idempotency cache (SQLite)
│   │   ├── review_queue.py      # Review queue + idempotent resolution replay (SQLite)
│   │   ├── confirmed_example_store.py # Few-shot confirmed examples (SQLite)
│   │   └── rule_store.py        # Tenant vendor→CoA rules (JSON-backed + runtime JSON overlay)
│   └── adapters/
│       └── accounting_sync.py   # Mock accounting platform sync
├── data/
│   ├── tenants.json             # Tenant configs + thresholds
│   ├── runtime/                 # Local runtime state (gitignored): SQLite DB + promoted rules
│   ├── coa/
│   │   ├── tenant_a.json        # Tenant A CoA
│   │   └── tenant_b.json        # Tenant B CoA
│   └── rules/
│       ├── tenant_a_rules.json  # Learned vendor rules for tenant A
│       └── tenant_b_rules.json
├── scripts/
│   ├── demo_scenario.py         # Runnable end-to-end demo (see §10)
│   └── smoke_test.sh            # Optional curl smoke against a running server
├── tests/
│   ├── test_rule_engine.py
│   ├── test_validator.py
│   ├── test_router.py
│   └── eval/
│       ├── eval_runner.py       # Offline eval harness
│       └── fixtures/
│           └── edge_cases.json  # Long-tail vendor eval dataset
├── pytest.ini                   # pytest import path hardening (repo root)
├── requirements.txt
└── README.md
```

### 2.4 MVP API contracts (FastAPI)

The MVP is intentionally API-first: the “transaction event stream” is simulated by HTTP endpoints. All endpoints are tenant-scoped via `tenant_id` in the request body or path.

**Authentication (MVP)**: tenant-scoped reads and writes require the `X-API-Key` header matching the per-tenant key in `data/tenants.json`. This is intentionally simple for the take-home; production would use OAuth2/mTLS/service accounts.

#### 2.4.1 Classify/tag a transaction

- **Method**: `POST`
- **Route**: `/transactions/tag`
- **Request body**: `Transaction` (see §5.1)
- **Response**: `TaggingResult` (see §5.4)

Success semantics:

- The endpoint always returns a `TaggingResult` even when the LLM fails, by routing to `UNKNOWN` (business-safe refusal). LLM failures are not surfaced as 5xx API failures.
- If `idempotency_key` has already been processed for this tenant, the endpoint returns the previously computed `TaggingResult`.

Error codes (minimal MVP set):

- **422 Unprocessable Entity**: request schema validation failed (Pydantic).
- **404 Not Found**: `tenant_id` does not exist.
- **409 Conflict**: `idempotency_key` was previously seen but the payload differs (guards against accidental reuse).

#### 2.4.2 Review queue actions (HITL)

- **List review items**
  - **Method**: `GET`
  - **Route**: `/review-queue/{tenant_id}`
  - **Response**: list of pending items (implementation detail; at minimum includes `tx_id`, suggested `coa_account_id`, `confidence`, `reasoning`)
- **Resolve a queued item**
  - **Method**: `POST`
  - **Route**: `/review-queue/{tx_id}/resolve`
  - **Request body**: `{ "tenant_id": "...", "action": "accept" | "correct", "final_coa_account_id": "..." }`
  - **Response**: updated `TaggingResult` + rule creation metadata if applicable
  - **422**: if `final_coa_account_id` is not in tenant CoA

Tenant-isolation hardening details are tracked in §9 (**Review resolve route hardening**) and `ARCHITECTURE.md` (API contracts + production boundary).

---

## 3. How This Addresses Workflow 1 Challenges

This MVP addresses Workflow 1 using a strict safety-first control loop:

- **Rule-first deterministic tagging** for repeat vendors (`source=rule`, `confidence=1.0`).
- **Tenant-scoped classification** for unseen vendors (CoA injected and hard-validated server-side).
- **Confidence routing** (`AUTO_TAG` / `REVIEW_QUEUE` / `UNKNOWN`) with cold-start tightening.
- **Learning loop** where reviewer outcomes improve future deterministic coverage.
- **No silent failures**: invalid/unavailable outputs always route to safe refusal paths.

For full flow details, invariants, and failure semantics, see `ARCHITECTURE.md` sections:

- **2) End-to-End Flow**
- **3) Architectural Invariants**
- **6) Failure-Mode Strategy**

---

## 4. LLM Prompt Design

> **Source of truth**: the authoritative prompt used at runtime is `app/pipeline/llm_prompt.py`. The snippets below are representative for reviewer readability and may be shortened for documentation.

### 4.1 System Prompt

> **Decoding parameters**: `temperature=0` is used for all classification calls. This maximises reproducibility — the same vendor + CoA list should always produce the same account selection. Non-zero temperature is appropriate for generative tasks but wrong for deterministic classification.
>
> **Tenant name**: `tenant_name` is loaded from `TenantConfig` (e.g., `data/tenants.json`) keyed by `tenant_id` during prompt construction. It is included for human debuggability; the LLM does not require `tenant_id` to classify correctly.

```text
You are a financial transaction classifier for a multi-tenant expense platform.
Return ONLY raw JSON with keys: reasoning, coa_account_id, confidence.
coa_account_id MUST come from the provided tenant CoA.
If ambiguous, keep confidence below 0.5; confident matches should be >= 0.85.
Choose the most specific plausible account; if tie remains, pick lower-risk and lower confidence.
```

For the exact live prompt text (including formatting and all guardrails), see `app/pipeline/llm_prompt.py`.

### 4.2 User Prompt (per transaction)

```
Classify the following transaction.

TRANSACTION:
- Vendor: {vendor_normalized}
- Amount: {amount} {currency}
- Date: {date}
- Card/Bill: {transaction_type}
- OCR Receipt Snippet: {ocr_text if available, else "Not available"}

HISTORICAL EXAMPLES FOR THIS TENANT:
{few_shot_examples}

Respond with ONLY a JSON object: {"coa_account_id": "...", "confidence": 0.0, "reasoning": "..."}
```

### 4.3 Few-Shot Example Format

```json
[
  {
    "vendor": "aws-marketplace",
    "amount": 1240.0,
    "currency": "USD",
    "coa_account_id": "6200",
    "coa_name": "Cloud & Hosting",
    "reasoning": "AWS Marketplace is cloud infrastructure spend."
  },
  {
    "vendor": "grab-sg-0023",
    "amount": 18.5,
    "currency": "SGD",
    "coa_account_id": "7200",
    "coa_name": "Local Transport",
    "reasoning": "Grab is a ride-hailing service; categorized as local transport."
  }
]
```

Up to 5 examples are injected from tenant-confirmed history using deterministic seeded sampling (`sha256(tx_id)`) for reproducibility in tests/demos. In production, this would be replaced by retrieval-based selection (most semantically similar vendor category), which improves long-tail performance — see §9.

**MVP few-shot selection spec**:

- **Source pool**: tenant’s historical, human-confirmed tags (e.g., prior `TaggingResult` with a final `coa_account_id` from the review/override path).
- **Sampling**: uniform random sample of up to 5 examples.
- **Exclusions**:
  - exclude examples whose normalized vendor key equals the current transaction’s vendor key
  - exclude malformed/incomplete examples
- **Reproducibility (implemented)**: sampling uses a stable `sha256(tx_id)`-derived seed so demo output is deterministic across runs and Python processes.

### 4.4 Expected LLM Response

```json
{
  "coa_account_id": "6200",
  "confidence": 0.93,
  "reasoning": "Vendor 'aws-marketplace' is a cloud infrastructure provider; matches account 6200 (Cloud & Hosting)."
}
```

---

## 5. Data Schemas & Contracts

### 5.1 Transaction Input

```python
class Transaction(BaseModel):
    tx_id: str
    tenant_id: str
    vendor_raw: str                      # raw merchant string from card network
    amount: Decimal                      # minor-unit handling for non-standard currencies (JPY=0 decimals, BHD=3) is delegated to the accounting sync adapter, which knows the target ledger's expectations
    currency: str                        # ISO 4217
    date: date
    transaction_type: Literal["card", "bill"]
    ocr_text: Optional[str] = None       # OCR'd receipt snippet
    idempotency_key: str                 # for safe retries
```

### 5.2 CoA Account

```python
class CoAAccount(BaseModel):
    account_id: str
    name: str
    description: str
    parent_id: Optional[str] = None      # for hierarchical CoA structures
```

### 5.3 LLM Output (validated before routing)

```python
class LLMClassificationOutput(BaseModel):
    coa_account_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
```

### 5.4 Tagging Result (audit record)

```python
class TaggingResult(BaseModel):
    tx_id: str
    tenant_id: str
    status: Literal["AUTO_TAG", "REVIEW_QUEUE", "UNKNOWN"]
    source: Literal["rule", "llm", "unknown"]
    coa_account_id: Optional[str]
    confidence: Optional[float]          # None when status=UNKNOWN and no LLM was called (e.g., empty vendor); 0.0 when LLM was called but all providers failed
    reasoning: Optional[str]
    timestamp: datetime
    idempotency_key: str
```

### 5.5 Vendor Rule

```python
class VendorRule(BaseModel):
    tenant_id: str
    vendor_key: str                      # normalized vendor string
    coa_account_id: str
    created_by: Literal["reviewer", "import"]
    created_at: datetime
    source_tx_id: Optional[str]          # the transaction that generated this rule
```

---

## 6. Failure Modes & Edge Cases

Full failure-mode policy is maintained in `ARCHITECTURE.md` (**6) Failure-Mode Strategy**).

Critical MVP behavior to remember:

- **Provider 4xx** -> no cross-provider fallback; safe refusal path.
- **Provider 429 / 5xx / timeout** -> bounded retry/fallback, then safe refusal if exhausted.
- **Invalid output or out-of-CoA account** -> `UNKNOWN`.
- **Replay conflicts** (`idempotency_key` or review resolve replay payload mismatch) -> `409`.
- **Low confidence** -> `REVIEW_QUEUE` / `UNKNOWN`, never forced `AUTO_TAG`.

---

## 7. Evaluation Strategy

### 7.1 Offline Eval Dataset

Located at `tests/eval/fixtures/edge_cases.json`. Structure:

```json
[
  {
    "tx_id": "eval_001",
    "tenant_id": "tenant_a",
    "vendor_raw": "PTTEP THAILAND FUEL 0049",
    "amount": 340.0,
    "currency": "THB",
    "expected_status": "REVIEW_QUEUE",
    "expected_coa_account_id": null,
    "difficulty": "long-tail",
    "note": "Obscure Thai fuel vendor; should not auto-tag"
  }
]
```

### 7.2 Metrics

| Metric                     | Definition                                                                                                                                                  | Target                                       |
| :------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------- |
| **Auto-tag precision**     | Correct CoA / total auto-tagged (measured against `edge_cases.json` fixture set, not live traffic — production ground truth requires human-reviewed labels) | ≥ 98% on fixture set                         |
| **Long-tail UNKNOWN rate** | `long-tail` fixtures routed to REVIEW or UNKNOWN / total `long-tail`                                                                                        | ≥ 90%                                        |
| **Review rate**            | Transactions routed to REVIEW / total                                                                                                                       | Informational (tracks threshold calibration) |
| **Confidence calibration** | Is conf=0.85 right ~85% of the time?                                                                                                                        | Brier score ≤ 0.05                           |
| **Rule coverage**          | Transactions handled by deterministic rule / total                                                                                                          | Trending up over time                        |

**Known gap — LLM confidence calibration**: LLM self-reported confidence is not natively well-calibrated; a model returning 0.85 is not reliably correct 85% of the time. Brier score ≤ 0.05 is the target eval metric. If calibration drifts in production, a post-hoc calibration layer (Platt scaling or isotonic regression, fit on the eval fixture set) can be inserted between Step 5 (Output Validation) and Step 6 (Confidence Router) without changing the surrounding architecture.

### 7.3 Regression Gate

A prompt change, model upgrade, or threshold adjustment must pass the eval harness before deployment:

- Auto-tag precision must not drop below 98%.
- Long-tail UNKNOWN rate must not drop below 90%.
- Any regression on either metric blocks the change, even if overall accuracy improves.
- Release governance: these checks are expected to run in CI; production prompt/model changes should not deploy unless the gate remains green.

### 7.4 Running Evals

```bash
python tests/eval/eval_runner.py --tenant tenant_a --fixture tests/eval/fixtures/edge_cases.json
python tests/eval/eval_runner.py --tenant tenant_b --fixture tests/eval/fixtures/edge_cases.json
```

Example output (as of the current `edge_cases.json` harness — 20 fixtures per tenant):

```text
Eval results - tenant_a
  Total fixtures:          20
  Auto-tag precision:      100.0%
  Long-tail UNKNOWN rate:  100.0%
  Review rate:             30.0%
  Brier score:             0.030
  Rule coverage:           10.0%
```

```text
Eval results - tenant_b
  Total fixtures:          20
  Auto-tag precision:      n/a (no AUTO_TAG fixtures in this run)
  Long-tail UNKNOWN rate:  100.0%
  Review rate:             65.0%
  Brier score:             0.040
  Rule coverage:           0.0%
```

Note: `tenant_b` is configured with `cold_start: true`, which raises the effective auto-post threshold; the fixture set is intentionally biased toward **REVIEW_QUEUE** / **UNKNOWN** rather than **AUTO_TAG**.

---

## 8. Architectural & Technology Choices

| Layer                | Choice                                             | Rationale                                                                                                                                                                                           |
| :------------------- | :------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Framework            | FastAPI                                            | Async-native, schema-first with Pydantic, minimal boilerplate, easy to test                                                                                                                         |
| Validation           | Pydantic v2                                        | Strict schema enforcement at I/O boundaries; catches LLM output errors before routing                                                                                                               |
| Primary LLM          | Google Gemini (`GOOGLE_API_KEY`)                   | Single key covers both transaction classification and Google Cloud OCR services (Document AI / Vision); reduces credential sprawl                                                                   |
| Fallback LLM         | Anthropic Claude (`CLAUDE_API_KEY`)                | Invoked automatically if Gemini errors or times out; strong JSON compliance and instruction-following                                                                                               |
| Second Fallback LLM  | OpenAI GPT-4o (`OPENAI_API_KEY`)                   | Final safety net if both Gemini and Claude are unavailable; ensures the pipeline never hard-fails on a single provider outage                                                                       |
| LLM Abstraction      | LiteLLM                                            | Standardizes API calls across all three providers using the OpenAI request format; the fallback chain is ~5 lines of code rather than three separate SDKs with divergent timeout and error handling |
| Storage (MVP)        | SQLite (`data/runtime/state.db`) + JSON seed files | Durable-enough local persistence for audit/idempotency/review/few-shot examples without standing up Postgres                                                                                        |
| Storage (production) | Postgres + Redis + append-only audit table         | See §9                                                                                                                                                                                              |
| Vendor normalization | Regex + `str.lower()` + whitespace collapse        | Deterministic, fast, no external dependency                                                                                                                                                         |

Design rationale and trade-off depth (provider semantics, abstraction boundaries, concurrency constraints) are documented in `ARCHITECTURE.md` sections:

- **4) System Components**
- **6) Failure-Mode Strategy**
- **7) MVP vs Production Boundary**

---

## 9. Production-Readiness Considerations

Canonical production architecture direction is in `ARCHITECTURE.md` (**7) MVP vs Production Boundary** and **8) Open Production Architecture Questions**.

### Observability & Debug Replay (implementation-oriented)

For each tagging decision, capture enough context to replay and diagnose outcomes quickly:

- **Trace identifiers**: `tx_id`, `tenant_id`, `idempotency_key`, timestamp.
- **Decision path**: `source` (`rule` / `llm` / `unknown`), final `status`, `confidence`, `coa_account_id`.
- **LLM attribution**: `provider_name`, `latency_ms`, `prompt_tokens`, `completion_tokens`, `total_tokens` (when live provider calls are used).
- **Failure reasoning**: validator/router/provider error category (for example `provider_4xx`, invalid CoA, below threshold).

**Debug replay workflow (MVP):**

1. Look up the record in `/audit-log/{tenant_id}` by `tx_id`.
2. Verify whether it came from deterministic rule path or classifier path (`source` + `provider_name`).
3. Re-run the same payload with a new `idempotency_key` in local/dev to reproduce behavior (or confirm idempotency replay behavior with the same key).
4. Use classifier/provider logs for upstream 4xx/5xx diagnostics when `source="unknown"` with provider errors.

### Must-haves before production

- **Immutable audit log**: every decision written to an append-only store with `tx_id`, `timestamp`, `source`, `confidence`, `coa_account_id`, and `reasoning`. No record is ever updated — corrections create new records.
- **Idempotency**: transactions are deduplicated by `idempotency_key` before entering the pipeline. Safe to retry on network failure.
- **PII stripping**: strip cardholder name, last 4 digits, and any personal identifiers from the `ocr_text` field before the LLM call.
- **Input guardrails**: enforce request-size limits before prompt construction (`vendor_raw` max 500 chars, `ocr_text` max 2000 chars) to prevent malformed payload amplification and provider-side 4xx churn.
- **Observability per tenant**:
  - Auto-tag rate (trending)
  - Review rate (trending)
  - UNKNOWN rate + top unknown vendors (actionable: which vendors need rules?)
  - LLM latency p50/p95
  - Token usage per request (cost control)
  - Decision-path tracing (`rule` vs `llm` vs `unknown`) with provider attribution
  - Alerting on drift thresholds (for example UNKNOWN spikes or Brier score degradation)
- **Eval harness**: run on every prompt or model change (see §7).
- **Tenant isolation**: every data access (CoA, rules, audit log, review queue) is scoped by `tenant_id` with server-side enforcement. A tenant must never be able to read or influence another tenant's data, rules, or audit trail — enforced at the repository layer, not just the API layer.
- **Review resolve route hardening (HIGH before external multi-tenant rollout)**: current resolve route carries `tenant_id` in request body (`POST /review-queue/{tx_id}/resolve`). Before real multi-tenant exposure, move tenant scope into the path (`POST /tenants/{tenant_id}/review-queue/{tx_id}/resolve`) and keep server-side tenant ownership checks.
- **Rate limiting**: protect the LLM call path from burst traffic; queue overflow to REVIEW rather than dropping.
- **Per-tenant LLM spend cap**: a configurable monthly token budget per tenant; transactions arriving after the cap is reached are routed to `REVIEW_QUEUE` rather than triggering LLM calls, preventing runaway costs for high-volume or misconfigured tenants.  
  _Status in this MVP_: planned and documented, not yet enforced in runtime code.
- **Confidence calibration operations**: run eval harness as a weekly CI job (non-blocking) and assign ownership to ML/platform on-call. If Brier score rises above `0.08`, block threshold/model changes and run a recalibration pass (Platt scaling or isotonic regression) before release.

### Next-step architecture (post-MVP)

- **Postgres**: tenants, CoA, rules, transactions, audit log.
- **Redis + queue (e.g., SQS)**: async processing, retries, review workflow.
- **Vector store (e.g., pgvector)**: long-tail vendor retrieval — find the 5 most similar historical transactions to the current one and inject as few-shot examples. This replaces the current random-sample few-shot approach.
- **RAG over confirmed tags**: as the tagged transaction corpus grows, retrieval-augmented few-shot selection will improve long-tail performance measurably. This is the natural next step after deterministic rules.

---

## 10. How to Run the MVP

### Prerequisites

- Python 3.10+
- **No LLM keys are required** to run tests, the demo script, or the offline eval harness: when `LLM_ENABLE_LIVE_CALLS=false` (default), the service uses a deterministic classifier path for stable CI and local development.
- **Optional live provider calls**: set `LLM_ENABLE_LIVE_CALLS=true` and provide one or more provider keys. The system dynamically builds the fallback chain based on whichever keys are present:
  - `GOOGLE_API_KEY` — primary LLM (Gemini) + Google Cloud services (Document AI / Vision OCR)
  - `CLAUDE_API_KEY` — fallback LLM (Claude)
  - `OPENAI_API_KEY` — fallback LLM (GPT-4o)
- Recommended priority order if you have multiple keys: `GOOGLE_API_KEY` → `CLAUDE_API_KEY` → `OPENAI_API_KEY`

### Setup

```bash
cd auto-tagging-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # optional: add provider keys + LLM_ENABLE_LIVE_CALLS=true for live calls
```

`.env.example`:

```env
# Live provider calls are opt-in for deterministic local dev/tests.
# Set to true when you want real provider chaining.
LLM_ENABLE_LIVE_CALLS=false

# Optional provider keys (used only when LLM_ENABLE_LIVE_CALLS=true).
# The fallback chain is built dynamically from whichever keys are present.
# Recommended order: Gemini → Claude → OpenAI.

# Primary LLM + Google Cloud services (Document AI, Vision OCR)
# GOOGLE_API_KEY=your_google_api_key_here

# Fallback LLM — invoked if Gemini fails or times out
# CLAUDE_API_KEY=your_claude_api_key_here

# Second fallback LLM — invoked if both Gemini and Claude are unavailable
# OPENAI_API_KEY=your_openai_api_key_here
```

### Run the API server

```bash
uvicorn app.main:app --reload
```

API docs available at `http://localhost:8000/docs`.

### Run tests

```bash
pytest
```

Note: test runs are intentionally deterministic. `tests/conftest.py` forces `LLM_ENABLE_LIVE_CALLS=false` so pytest results remain stable even if your shell or `.env` enables live provider calls.

If your environment does not pick up `pytest.ini` for some reason, this also works:

```bash
python -m pytest
```

### Optional HTTP smoke test (requires a running server)

```bash
uvicorn app.main:app --reload
bash scripts/smoke_test.sh
```

### Demo scenario (recommended starting point)

Runs 4 transactions through the full pipeline and prints audit output to terminal:

```bash
python scripts/demo_scenario.py
```

**What the demo shows:**

```text
[Audit] tx=101 vendor=zoom-us        source=rule  conf=1.00 -> AUTO_TAG    account=6100 (SaaS Tools)
[Audit] tx=102 vendor=grab-sg-0023   source=llm   conf=0.65 -> REVIEW_QUEUE
         reasoning: "Grab is a ride-hailing service; could be Local Transport or Client Entertainment."

--- Reviewer corrects tx=102: account=7200 (Local Transport) ---
[Audit] tx=102 reviewer_override     final=7200   rule_created vendor_key=grab-sg-0023

[Audit] tx=103 vendor=grab-sg-0091   source=rule  conf=1.00 -> AUTO_TAG    account=7200 (Local Transport)
         (LLM bypassed — deterministic rule from reviewer correction)

[Audit] tx=104 vendor=PTTEP THAI 049 source=llm   conf=0.31 -> UNKNOWN
         reasoning: "Vendor is ambiguous; insufficient signal to classify confidently."
```

This output demonstrates the full safety story: rule-based auto-tag, LLM with medium confidence routed to review, learning loop creating a new rule, subsequent identical vendor bypassing LLM, and low-confidence correctly routing to UNKNOWN.

### Key API endpoints

| Method | Endpoint                        | Description                      |
| :----- | :------------------------------ | :------------------------------- |
| `POST` | `/transactions/tag`             | Submit a transaction for tagging |
| `GET`  | `/review-queue/{tenant_id}`     | List pending review items        |
| `POST` | `/review-queue/{tx_id}/resolve` | Accept or correct a suggestion   |
| `GET`  | `/audit-log/{tenant_id}`        | View full audit trail            |
| `GET`  | `/rules/{tenant_id}`            | View current vendor rule store   |

---

## 11. Explicit Assumptions

- **Standard cloud infrastructure is available**: Compute, networking, secrets management, and managed storage primitives are assumed to exist for deployment. The MVP focuses on workflow logic and safety controls, not infrastructure provisioning.
- **CoA tagging only (MVP scope)**: The prompt specifies tax codes, tracking categories, and required metadata. The 4–6 hour time budget makes full implementation impractical without sacrificing correctness. Tax codes and tracking categories are architecturally anticipated but deferred.
- **Transaction event stream**: Simulated by the `POST /transactions/tag` HTTP endpoint and the demo script. In production this would be a Kafka or SQS consumer.
- **Accounting platform sync**: Represented by a mock adapter that logs the payload. Real integration (Xero, QuickBooks, NetSuite) is an adapter swap.
- **Vendor rules are keyed by normalized vendor string**: Lowercased, whitespace collapsed, punctuation stripped. E.g., `"AWS Marketplace, Inc."` → `"aws marketplace inc"`. Fuzzy matching is a next-step improvement.
- **Few-shot examples are sampled from confirmed tenant history**: For the MVP, up to 5 examples are selected via deterministic seeded sampling (`sha256(tx_id)`). In production, retrieval-based selection (most similar vendor category) is better.
- **Storage is local JSON seed files + SQLite runtime state**: sufficient for a take-home MVP demo; production storage choices are documented in §9.
- **Deployment mode for this MVP is single-process only**: in-process `RLock` + SQLite file locking protects correctness for local/demo runs, but is not a substitute for distributed concurrency control across multiple workers/replicas.
- **Single primary LLM call per transaction**: fallback providers are only invoked if the primary errors or times out. For the MVP's narrow scope (CoA tagging only), a single well-structured prompt is sufficient; in the worst case (all prior providers fail) up to 3 calls are made, each with the same prompt and schema.
- **Rule writes are idempotent on `(tenant_id, vendor_key)`**: if two transactions for the same new vendor arrive simultaneously and both trigger reviewer corrections concurrently, the second write is a no-op (last-write-wins on an identical key). This prevents duplicate rule entries and makes the rule store safe under concurrent correction.
- **No PII in demo data**: Demo fixtures use synthetic data. PII scrubbing is listed as a production must-have in §9.

---

## 12. Open Questions for Production Scaling (Post-MVP)

Canonical open production architecture questions are maintained in `ARCHITECTURE.md` under **8) Open Production Architecture Questions**.
