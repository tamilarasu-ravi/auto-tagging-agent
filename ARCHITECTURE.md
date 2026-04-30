# Architecture notes

## MVP vs production

| Concern | MVP (this repo) | Production direction |
|--------|------------------|----------------------|
| **Orchestration** | `TaggingService` in `app/services/`; FastAPI routes in `app/main.py` stay thin | Same boundary; add application unit-of-work / transactions where needed |
| **Persistence** | SQLite file (`data/runtime/state.db`) for idempotency, audit, review queue, confirmed examples | Postgres (or managed SQL) with migrations, backups, and row-level tenant isolation |
| **Auth** | Static `X-API-Key` per tenant in `data/tenants.json` | OAuth2 / API gateway / mTLS; key rotation and audit of credential use |
| **LLM** | LiteLLM + opt-in live chain (`LLM_ENABLE_LIVE_CALLS`); prompt/parse/provider split under `app/pipeline/llm_*.py` | Managed inference endpoints, budget caps, circuit breakers, structured output contracts |
| **PII** | Regex OCR redaction in `app/pipeline/preprocessor.py` | DLP pipeline, field-level classification, retention policies |
| **Observability** | `logging` on decision paths | OpenTelemetry, metrics (auto-tag / review / unknown rates), PII-safe log fields |
| **Scaling** | `threading.RLock` + single DB file | Horizontally scaled workers, queue-backed tagging, idempotent consumers |

**MVP deployment constraint**: this implementation is intended for a single-process runtime. `threading.RLock` is process-local, so it does not coordinate concurrent writers across multiple worker processes or replicas.

For the full product narrative and API contracts, see `README.md`.
