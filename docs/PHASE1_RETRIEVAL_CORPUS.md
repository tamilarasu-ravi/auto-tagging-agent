# Phase 1 — Retrieval corpus (data model)

This document describes the **retrieval corpus** table and APIs added in Phase 1. It is the **ingestion surface** for future RAG (Phase 2); it does **not** change classification or few-shot selection yet.

## Purpose

- Persist **human-confirmed** final CoA labels with enough context (vendor, amount, currency, date, type) to build **embedding text** and **metadata filters** later.
- Stay **tenant-isolated** and **append-oriented** at the row level (one logical row per `(tenant_id, tx_id)` from a successful review resolve).

## Schema (`retrieval_corpus` in `data/runtime/state.db`)

| Column | Notes |
|--------|--------|
| `corpus_id` | INTEGER PK |
| `tenant_id` | Scope |
| `tx_id` | With `tenant_id`, **UNIQUE** — idempotent insert safety |
| `vendor_key` | Normalized vendor (same as rule engine key) |
| `vendor_raw` | Original merchant string (optional; null for legacy queue rows) |
| `amount`, `currency` | Optional strings from transaction |
| `transaction_date` | ISO date text or NULL |
| `transaction_type` | `card` / `bill` or NULL |
| `final_coa_account_id` | Reviewer-chosen final account |
| `suggested_coa_account_id` | Classifier suggestion before resolve |
| `confidence` | Suggestion confidence at queue time |
| `resolution_action` | `accept` or `correct` |
| `idempotency_key` | From original transaction |
| `created_at` | UTC ISO timestamp |
| `embedding_model`, `embedding_version` | Reserved for Phase 2 (always NULL for now) |

## Write path

On **first successful** `POST /review-queue/{tx_id}/resolve` (not on idempotent replay), after `ConfirmedExampleStore.add_example`:

1. Build `RetrievalCorpusInsert` from `ReviewQueueItem` + `ReviewResolveRequest`.
2. `RetrievalCorpusStore.insert` uses `INSERT OR IGNORE` on `(tenant_id, tx_id)`.

`ReviewQueueItem` now carries optional **`vendor_raw`**, **`amount`**, **`currency`**, **`transaction_date`**, **`transaction_type`** when enqueued from `POST /transactions/tag`, so the corpus row can be rich without re-reading the original transaction.

## Read path

- **`GET /corpus/{tenant_id}`** — same **`X-API-Key`** auth as other tenant routes; optional query params `limit` (default 200, max 500), `offset`.
- Returns `RetrievalCorpusDocument` JSON list, newest `corpus_id` first.
- **Not** wired into `LLMClassifier` or prompts in Phase 1.

## What is explicitly out of scope (Phase 1)

- Embeddings, vector indexes, retrieval in prompts.
- Writes from classifier `AUTO_TAG` or rule-only `AUTO_TAG` (only **review resolve** today).
- Postgres migration (still SQLite for MVP).

## Phase 2 handoff

- Build embedding string from: `tenant_id`, `vendor_key` / `vendor_raw`, `currency`, `transaction_type`, optional amount bucket, `final_coa_account_id`.
- Backfill or dual-write from `confirmed_example` if needed for older environments.
