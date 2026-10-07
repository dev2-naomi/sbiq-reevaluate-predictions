#!/usr/bin/env bash
# Post-deploy smoke tests. Usage:
#   API_URL=https://xxx.lambda-url.us-east-2.on.aws ./scripts/smoke_test_aws.sh
set -eo pipefail

API_URL="${API_URL:?Set API_URL to the CDK ApiUrl output}"

_curl() {
  if [[ -n "${API_KEY:-}" ]]; then
    curl -sf -H "x-api-key: $API_KEY" "$@"
  else
    curl -sf "$@"
  fi
}

echo "==> GET /health"
_curl "$API_URL/health" | grep -q '"status":"ok"' || _curl "$API_URL/health" | grep -q '"status": "ok"'

echo "==> POST /assistants/search"
_curl -X POST "$API_URL/assistants/search" \
  -H "Content-Type: application/json" -d '{}' | grep -q reevaluate-preconditions

echo ""
echo "Basic smoke tests passed."
echo "For a full end-to-end run, use test_cloud.py pointed at this API:"
echo "  REEVALUATE_LANGGRAPH_URL=$API_URL python3 test_cloud.py --predicted <file> --manifest <file>"
