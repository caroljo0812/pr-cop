#!/usr/bin/env bash
# POST the bundled sample diff to a running PR Cop server.
# Assumes:  prcop serve  is running at http://localhost:8080.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="${PRCOP_URL:-http://localhost:8080}"

if ! command -v jq >/dev/null 2>&1; then
  echo "this example needs jq (to JSON-encode the diff body)" >&2
  exit 1
fi

BODY="$(jq -Rs --arg specialists "security,performance" '
  {diff: ., title: "Add /users and /admin handlers", specialists: ($specialists | split(","))}
' < "$HERE/sample.diff")"

curl -sS "$URL/review/diff" \
  -H 'content-type: application/json' \
  -d "$BODY" \
  | jq '{verdict, finding_count, findings: [.findings[] | {severity, specialist, file, line, title}]}'
