#!/usr/bin/env bash
# Same as review-curl.sh but sends X-PRCOP-API-Key for servers that have
# PRCOP_API_KEY set. Pass the key via env: PRCOP_API_KEY=... bash review-curl-authed.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${PRCOP_URL:-http://localhost:8080}"

if [[ -z "${PRCOP_API_KEY:-}" ]]; then
  echo "set PRCOP_API_KEY=... in your env first" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "this example needs jq" >&2
  exit 1
fi

BODY="$(jq -Rs '{diff: ., title: "Add /users and /admin handlers"}' < "$HERE/sample.diff")"

curl -sS "$URL/review/diff" \
  -H 'content-type: application/json' \
  -H "X-PRCOP-API-Key: $PRCOP_API_KEY" \
  -d "$BODY" \
  | jq '{verdict, finding_count}'
