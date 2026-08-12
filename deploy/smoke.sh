#!/usr/bin/env bash
# Verifies a running HiveMCP container actually works, rather than just that the
# process started. Run after `make up`.
#
#   ./deploy/smoke.sh [base-url]

set -euo pipefail

BASE="${1:-http://localhost:8080}"

# Pick up HIVE_AUTH_TOKEN from .env so the script works without exporting it by hand.
ENV_FILE="$(dirname "$0")/../.env"
if [[ -f "$ENV_FILE" ]] && [[ -z "${HIVE_AUTH_TOKEN:-}" ]]; then
    HIVE_AUTH_TOKEN="$(sed -n 's/^HIVE_AUTH_TOKEN=//p' "$ENV_FILE" | tail -1)"
fi

# The array is seeded with a real header rather than left empty, because macOS still
# ships bash 3.2, where expanding an empty array under `set -u` is an "unbound variable"
# error. Later bash versions allow it. The header also makes these requests easy to spot
# in `make logs`.
CURL_ARGS=(-H "X-Hive-Smoke: 1")
if [[ -n "${HIVE_AUTH_TOKEN:-}" ]]; then
    CURL_ARGS+=(-H "Authorization: Bearer ${HIVE_AUTH_TOKEN}")
fi

pass=0
fail=0

check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        printf '  \033[32mPASS\033[0m %-46s %s\n' "$name" "$actual"
        pass=$((pass + 1))
    else
        printf '  \033[31mFAIL\033[0m %-46s got %s, want %s\n' "$name" "$actual" "$expected"
        fail=$((fail + 1))
    fi
}

contains() {
    local name="$1" needle="$2" haystack="$3"
    if [[ "$haystack" == *"$needle"* ]]; then
        printf '  \033[32mPASS\033[0m %-46s found %s\n' "$name" "$needle"
        pass=$((pass + 1))
    else
        printf '  \033[31mFAIL\033[0m %-46s %s missing\n' "$name" "$needle"
        printf '        response: %s\n' "${haystack:0:200}"
        fail=$((fail + 1))
    fi
}

status() { curl -sS -o /dev/null -w '%{http_code}' "${CURL_ARGS[@]}" "$@" 2>/dev/null || echo "000"; }

# Reads one field out of a JSON response. Grepping for '"key":"value"' looked simpler
# but silently depends on the server emitting compact JSON: a single space after the
# colon, and every check fails while the response is plainly correct.
json_get() {
    printf '%s' "$1" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for part in sys.argv[1].split("."):
    if isinstance(value, dict):
        value = value.get(part)
    elif isinstance(value, list) and part.isdigit():
        value = value[int(part)] if int(part) < len(value) else None
    else:
        value = None
    if value is None:
        break
print("" if value is None else value)
' "$2" 2>/dev/null || true
}

echo "Smoke testing ${BASE}"
echo

printf '  waiting for readiness'
for _ in $(seq 1 30); do
    if [[ "$(status "${BASE}/healthz")" == "200" ]]; then
        break
    fi
    printf '.'
    sleep 1
done
echo
echo "-- basics"
check "GET /healthz"                   "200" "$(status "${BASE}/healthz")"
check "GET /readyz"                    "200" "$(status "${BASE}/readyz")"
check "GET /d/<garbage> rejected"      "403" "$(status "${BASE}/d/not-a-real-token")"
check "GET /nope is 404"               "404" "$(status "${BASE}/nope")"

echo
echo "-- OpenAPI tool surface"
schema="$(curl -sS "${CURL_ARGS[@]}" "${BASE}/openapi.json" 2>/dev/null || echo '{}')"
for tool in hive_create_presentation hive_create_document hive_create_spreadsheet; do
    contains "openapi.json advertises ${tool}" "$tool" "$schema"
done
# Health probes must stay out of the schema, or OpenWebUI offers them to the model
# as tools.
if [[ "$schema" == *'"/healthz"'* ]]; then
    printf '  \033[31mFAIL\033[0m %-46s /healthz leaked into the tool schema\n' "health routes hidden"
    fail=$((fail + 1))
else
    printf '  \033[32mPASS\033[0m %-46s health routes hidden\n' "health routes hidden"
    pass=$((pass + 1))
fi

echo
echo "-- end to end: generate a real .pptx"
result="$(curl -sS -X POST "${CURL_ARGS[@]}" \
    -H 'Content-Type: application/json' \
    -H 'X-Hive-User-Id: smoke-test' \
    -d '{"spec":{"title":"Smoke Test","slides":[{"layout":"title","title":"Es funktioniert","subtitle":"HiveMCP"},{"layout":"title_content","title":"Punkte","bullets":[{"text":"Rendern"},{"text":"Ausliefern"}]}]}}' \
    "${BASE}/tools/create_presentation" 2>/dev/null || echo '{}')"
check "tool call returns a filename"     "Smoke Test.pptx" "$(json_get "$result" filename)"
check "tool call reports 2 slides"      "2"               "$(json_get "$result" slide_count)"

url="$(json_get "$result" download_url)"
if [[ -n "$url" ]]; then
    # A .pptx is a zip; the PK signature proves a real file came back, not an error page.
    magic="$(curl -sS "$url" 2>/dev/null | head -c 2 || true)"
    check "download link serves a real file" "PK" "$magic"
else
    printf '  \033[31mFAIL\033[0m %-46s no download_url in the response\n' "download link"
    fail=$((fail + 1))
fi

echo
echo "-- MCP surface"
# The regression check for python-sdk #1367: a mount whose session manager was never
# started answers 404/500 here with nothing explaining why.
mcp="$(curl -sS -X POST "${CURL_ARGS[@]}" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' \
    "${BASE}/mcp/" 2>/dev/null || echo '')"
contains "MCP initialize answers" "HiveMCP" "$mcp"

echo
if [[ $fail -eq 0 ]]; then
    printf '\033[32m%d passed, 0 failed\033[0m\n' "$pass"
else
    printf '\033[31m%d passed, %d failed\033[0m\n' "$pass" "$fail"
    exit 1
fi
