#!/usr/bin/env bash
# Run PR Cop against the bundled sample diff using the local CLI.
# Defaults to the mock provider so this works offline.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${PRCOP_LLM_PROVIDER:=mock}"
export PRCOP_LLM_PROVIDER

echo ">>> reviewing $HERE/sample.diff with PRCOP_LLM_PROVIDER=$PRCOP_LLM_PROVIDER"
prcop review --diff "$HERE/sample.diff" --title "Add /users and /admin handlers"
