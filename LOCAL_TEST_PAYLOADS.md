# Local Test Payloads (All Core Scenarios)

This file contains copy-paste payloads for manual API testing in Swagger (`/docs`) or curl.

## How to use

- Start server: `uvicorn app.main:app --reload`
- Open: `http://localhost:8000/docs`
- Use unique `tx_id` and `idempotency_key` per run to avoid cache collisions.

---

## 1) `POST /transactions/tag` scenarios

### 1.1 Rule hit -> `AUTO_TAG` (source=`rule`)

```json
{
  "tx_id": "manual_tx_rule_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "Zoom US",
  "amount": "29.99",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_rule_001"
}
```

Expected: `status=AUTO_TAG`, `source=rule`, `coa_account_id=6100`, `confidence=1.0`.

### 1.2 Classifier high confidence -> `AUTO_TAG` (source=`llm`)

```json
{
  "tx_id": "manual_tx_llm_auto_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "AWS Marketplace",
  "amount": "420.00",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_llm_auto_001"
}
```

Expected (deterministic mode): `status=AUTO_TAG`, `source=llm`, `coa_account_id=6200`.

### 1.3 Classifier medium confidence -> `REVIEW_QUEUE`

```json
{
  "tx_id": "manual_tx_review_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "Grab SG 9001",
  "amount": "18.50",
  "currency": "SGD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_review_001"
}
```

Expected (deterministic mode): `status=REVIEW_QUEUE`, `source=llm`, `coa_account_id=7200`, `confidence~0.65`.

### 1.4 Low confidence / refuse -> `UNKNOWN`

```json
{
  "tx_id": "manual_tx_unknown_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "PTTEP THAILAND FUEL 0049",
  "amount": "340.00",
  "currency": "THB",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_unknown_001"
}
```

Expected (deterministic mode): `status=UNKNOWN`.

### 1.5 Tenant-specific CoA validation fail -> `UNKNOWN`

```json
{
  "tx_id": "manual_tx_tenant_b_invalid_001",
  "tenant_id": "tenant_b",
  "vendor_raw": "AWS Marketplace",
  "amount": "210.00",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_tenant_b_invalid_001"
}
```

Expected (deterministic mode): `status=UNKNOWN` (classifier output not in tenant_b CoA).

### 1.6 Unknown tenant -> `404`

```json
{
  "tx_id": "manual_tx_bad_tenant_001",
  "tenant_id": "tenant_x",
  "vendor_raw": "Zoom US",
  "amount": "20.00",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_bad_tenant_001"
}
```

Expected: `404` with `Unknown tenant_id`.

### 1.7 Request schema validation fail -> `422`

```json
{
  "tx_id": "manual_tx_schema_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "Zoom US",
  "amount": "20.00",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "wire",
  "ocr_text": null,
  "idempotency_key": "manual_idem_schema_001"
}
```

Expected: `422` (`transaction_type` must be `card` or `bill`).

---

## 2) Idempotency scenarios

### 2.1 Same payload + same key -> cached same result (`200`)

Send this exact payload twice:

```json
{
  "tx_id": "manual_tx_idem_same_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "Unknown Vendor LLC",
  "amount": "41.00",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_same_001"
}
```

Expected: same response body both times, both `200`.

### 2.2 Different payload + same key -> `409`

First request:

```json
{
  "tx_id": "manual_tx_idem_conflict_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "Unknown Vendor LLC",
  "amount": "41.00",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_conflict_001"
}
```

Second request (reuse same key, change amount):

```json
{
  "tx_id": "manual_tx_idem_conflict_001",
  "tenant_id": "tenant_a",
  "vendor_raw": "Unknown Vendor LLC",
  "amount": "42.00",
  "currency": "USD",
  "date": "2026-04-30",
  "transaction_type": "card",
  "ocr_text": null,
  "idempotency_key": "manual_idem_conflict_001"
}
```

Expected second response: `409`.

---

## 3) Review queue + resolve scenarios

### 3.1 List queue

Call: `GET /review-queue/tenant_a`

Expected: includes review items after running scenario 1.3.

### 3.2 Resolve `accept` -> `AUTO_TAG`, no rule creation

Assume queued tx id is `manual_tx_review_001`:

`POST /review-queue/manual_tx_review_001/resolve`

```json
{
  "tenant_id": "tenant_a",
  "action": "accept",
  "final_coa_account_id": "7200"
}
```

Expected: `result.status=AUTO_TAG`, `rule_created=false`.

### 3.3 Resolve `correct` -> `AUTO_TAG` + rule promotion

Create new queue item first (same as 1.3 but new ids), then resolve:

`POST /review-queue/manual_tx_review_002/resolve`

```json
{
  "tenant_id": "tenant_a",
  "action": "correct",
  "final_coa_account_id": "6100"
}
```

Expected: `result.status=AUTO_TAG`, `rule_created=true`.

### 3.4 Post-correction same vendor -> deterministic rule hit

Submit the same `vendor_raw` used in 3.3 with a new `tx_id` and `idempotency_key`.

Expected: `status=AUTO_TAG`, `source=rule`, `confidence=1.0`.

### 3.5 Resolve with invalid CoA -> `422`

`POST /review-queue/manual_tx_review_003/resolve`

```json
{
  "tenant_id": "tenant_a",
  "action": "correct",
  "final_coa_account_id": "9999"
}
```

Expected: `422`.

### 3.6 Resolve missing queue item -> `404`

`POST /review-queue/tx_does_not_exist/resolve`

```json
{
  "tenant_id": "tenant_a",
  "action": "accept",
  "final_coa_account_id": "6100"
}
```

Expected: `404`.

---

## 4) Read models / observability endpoints

### 4.1 Get audit log

Call: `GET /audit-log/tenant_a`

Expected: append-only event list for tenant_a.

### 4.2 Get rule store

Call: `GET /rules/tenant_a`

Expected: includes base rules + promoted runtime rules.

### 4.3 Unknown tenant reads -> `404`

Try:
- `GET /review-queue/tenant_x`
- `GET /audit-log/tenant_x`
- `GET /rules/tenant_x`

Expected: `404` for each.
