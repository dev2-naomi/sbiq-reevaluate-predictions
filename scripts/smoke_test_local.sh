#!/usr/bin/env bash
# Local smoke tests for the platform API (no AWS required).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export CHECKPOINT_IN_MEMORY=true
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif [[ -f venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

echo "==> Starting API server on :8080"
uvicorn api.main:app --host 127.0.0.1 --port 8080 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT

BASE="http://127.0.0.1:8080"
for i in $(seq 1 15); do
  if curl -sf "$BASE/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> GET /health"
curl -sf "$BASE/health" | grep -q '"status":"ok"' || curl -sf "$BASE/health" | grep -q '"status": "ok"'

echo "==> POST /assistants/search"
curl -sf -X POST "$BASE/assistants/search" -H "Content-Type: application/json" -d '{}' \
  | grep -q reevaluate-preconditions

echo "==> POST /threads"
THREAD=$(curl -sf -X POST "$BASE/threads" -H "Content-Type: application/json" -d '{}')
echo "$THREAD" | grep -q thread_id

echo "Local smoke tests passed."
echo "Note: this does NOT exercise a full reevaluate-preconditions run (requires a real"
echo "ANTHROPIC_API_KEY + realistic predicted-conditions/manifest input) — run"
echo "test_cloud.py against this API (with REEVALUATE_LANGGRAPH_URL=$BASE) for that."
