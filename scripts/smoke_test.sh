#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TENANT_A_KEY="${TENANT_A_KEY:-demo_key_tenant_a}"
TENANT_B_KEY="${TENANT_B_KEY:-demo_key_tenant_b}"
AUTO_START_SERVER="${AUTO_START_SERVER:-true}"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required."
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required."
  exit 1
fi

RUN_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4().hex[:8])
PY
)"

json_field() {
  local json="$1"
  local path="$2"
  python3 - "$json" "$path" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
path = sys.argv[2].split(".")
value = payload
for part in path:
    if part:
        value = value[part]
if value is None:
    print("null")
else:
    print(value)
PY
}

assert_eq() {
  local got="$1"
  local expected="$2"
  local message="$3"
  if [[ "$got" != "$expected" ]]; then
    echo "FAIL: $message (expected '$expected', got '$got')"
    exit 1
  fi
  echo "PASS: $message"
}

post_json() {
  local path="$1"
  local api_key="$2"
  local payload="$3"
  curl -sS -X POST "${BASE_URL}${path}" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: ${api_key}" \
    -d "${payload}"
}

get_json() {
  local path="$1"
  local api_key="$2"
  curl -sS -X GET "${BASE_URL}${path}" \
    -H "X-API-Key: ${api_key}"
}

health_ok() {
  curl -sS "${BASE_URL}/health" >/dev/null 2>&1
}

wait_for_health() {
  local timeout_s="${1:-10}"
  local start_ts
  start_ts="$(python3 - <<'PY'
import time
print(time.time())
PY
)"

  while ! health_ok; do
    local now_ts
    now_ts="$(python3 - <<'PY'
import time
print(time.time())
PY
)"
    if python3 - <<PY
import sys
start=float("${start_ts}")
now=float("${now_ts}")
timeout=float("${timeout_s}")
sys.exit(0 if (now-start) <= timeout else 1)
PY
    then
      sleep 0.2
      continue
    fi
    return 1
  done
  return 0
}

SERVER_PID=""
cleanup_server() {
  if [[ -n "${SERVER_PID}" ]]; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_server EXIT

echo "Running smoke tests against ${BASE_URL} (run_id=${RUN_ID})"

if ! health_ok; then
  if [[ "${AUTO_START_SERVER}" == "true" ]]; then
    echo "No server detected at ${BASE_URL}. Starting uvicorn for smoke tests..."
    # Run server in background for smoke tests only.
    python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning >/tmp/reap_smoke_${RUN_ID}.log 2>&1 &
    SERVER_PID="$!"
    if ! wait_for_health 15; then
      echo "FAIL: server did not become healthy in time. See /tmp/reap_smoke_${RUN_ID}.log"
      exit 1
    fi
  else
    echo "FAIL: could not reach ${BASE_URL}. Start the server first or set AUTO_START_SERVER=true."
    exit 1
  fi
fi

# 1) Rule hit
RULE_RESP="$(post_json "/transactions/tag" "${TENANT_A_KEY}" "{
  \"tx_id\": \"smoke_rule_${RUN_ID}\",
  \"tenant_id\": \"tenant_a\",
  \"vendor_raw\": \"Zoom US\",
  \"amount\": \"19.99\",
  \"currency\": \"USD\",
  \"date\": \"2026-04-30\",
  \"transaction_type\": \"card\",
  \"ocr_text\": null,
  \"idempotency_key\": \"smoke_idem_rule_${RUN_ID}\"
}")"
assert_eq "$(json_field "${RULE_RESP}" "status")" "AUTO_TAG" "rule route status"
assert_eq "$(json_field "${RULE_RESP}" "source")" "rule" "rule route source"

# 2) Review queue path
REVIEW_TX_ID="smoke_review_${RUN_ID}"
REVIEW_RESP="$(post_json "/transactions/tag" "${TENANT_A_KEY}" "{
  \"tx_id\": \"${REVIEW_TX_ID}\",
  \"tenant_id\": \"tenant_a\",
  \"vendor_raw\": \"Grab SG ${RUN_ID}\",
  \"amount\": \"18.50\",
  \"currency\": \"SGD\",
  \"date\": \"2026-04-30\",
  \"transaction_type\": \"card\",
  \"ocr_text\": null,
  \"idempotency_key\": \"smoke_idem_review_${RUN_ID}\"
}")"
assert_eq "$(json_field "${REVIEW_RESP}" "status")" "REVIEW_QUEUE" "review route status"

# 3) Resolve correction and rule promotion
RESOLVE_RESP="$(post_json "/review-queue/${REVIEW_TX_ID}/resolve" "${TENANT_A_KEY}" "{
  \"tenant_id\": \"tenant_a\",
  \"action\": \"correct\",
  \"final_coa_account_id\": \"6100\"
}")"
assert_eq "$(json_field "${RESOLVE_RESP}" "rule_created")" "True" "rule promotion flag"

# 4) Same vendor should now hit deterministic rule
POST_PROMOTE_RESP="$(post_json "/transactions/tag" "${TENANT_A_KEY}" "{
  \"tx_id\": \"smoke_post_promote_${RUN_ID}\",
  \"tenant_id\": \"tenant_a\",
  \"vendor_raw\": \"Grab SG ${RUN_ID}\",
  \"amount\": \"21.00\",
  \"currency\": \"SGD\",
  \"date\": \"2026-04-30\",
  \"transaction_type\": \"card\",
  \"ocr_text\": null,
  \"idempotency_key\": \"smoke_idem_post_promote_${RUN_ID}\"
}")"
assert_eq "$(json_field "${POST_PROMOTE_RESP}" "source")" "rule" "post-promotion rule source"
assert_eq "$(json_field "${POST_PROMOTE_RESP}" "status")" "AUTO_TAG" "post-promotion status"

# 5) tenant_b safety check (cold-start should avoid auto-posting)
TENANT_B_RESP="$(post_json "/transactions/tag" "${TENANT_B_KEY}" "{
  \"tx_id\": \"smoke_tenant_b_${RUN_ID}\",
  \"tenant_id\": \"tenant_b\",
  \"vendor_raw\": \"AWS Marketplace\",
  \"amount\": \"210.00\",
  \"currency\": \"USD\",
  \"date\": \"2026-04-30\",
  \"transaction_type\": \"card\",
  \"ocr_text\": null,
  \"idempotency_key\": \"smoke_idem_tenant_b_${RUN_ID}\"
}")"
assert_eq "$(json_field "${TENANT_B_RESP}" "status")" "REVIEW_QUEUE" "tenant_b conservative review (cold start)"

# 6) Auth check
AUTH_STATUS="$(curl -sS -o /tmp/smoke_auth_${RUN_ID}.json -w "%{http_code}" \
  -X GET "${BASE_URL}/rules/tenant_a" -H "X-API-Key: wrong_key")"
assert_eq "${AUTH_STATUS}" "403" "auth rejection on wrong key"

echo "All smoke checks passed."
