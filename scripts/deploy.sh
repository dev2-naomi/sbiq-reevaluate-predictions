#!/usr/bin/env bash
# Deploy the reevaluate-preconditions agent stack to AWS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "AWS credentials not configured. Run: aws sso login (or export AWS_PROFILE)"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker before deploying."
  exit 1
fi

cd "$ROOT/infra"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

# Load local .env so CDK can inject values into Lambda + Secrets Manager
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

# REEVALUATE_STAGE selects which stack this deploys: "prod" (default) or
# "dev" (a separate, -dev-suffixed stack; see infra/app.py and
# infra/stacks/reevaluate_stack.py).
export REEVALUATE_STAGE="${REEVALUATE_STAGE:-prod}"
if [[ "$REEVALUATE_STAGE" == "prod" ]]; then
  STACK_ID="ReevaluatePreconditionsStack"
else
  STACK_ID="ReevaluatePreconditionsStack-Dev"
fi

echo "Bootstrapping CDK (safe to re-run)..."
cdk bootstrap

echo "Deploying stack $STACK_ID (stage=$REEVALUATE_STAGE)..."
OUTPUTS_TMP="$(mktemp)"
cdk deploy "$STACK_ID" --require-approval never --outputs-file "$OUTPUTS_TMP"

python3 - <<'PY' "$OUTPUTS_TMP" "$ROOT/aws-output.json"
import json
import sys

src, dest = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    stacks = json.load(f)

flat: dict[str, str] = {}
for stack_name, outputs in stacks.items():
    for key, value in outputs.items():
        flat[f"{stack_name}.{key}"] = value

with open(dest, "w", encoding="utf-8") as f:
    json.dump(flat, f, indent=2)
    f.write("\n")
PY
rm -f "$OUTPUTS_TMP"

echo ""
echo "Wrote CDK outputs to $ROOT/aws-output.json"
cat "$ROOT/aws-output.json"
echo ""

# Push real secret values directly to Secrets Manager via the API — this is
# NOT a CloudFormation resource property, so it never appears in a stack
# template. CDK only seeds the secret with an empty placeholder. Keep this
# key list in sync with infra/stacks/reevaluate_stack.py:secret_keys and
# api/secrets.py:SECRET_KEYS.
SECRET_ARN="$(python3 -c "
import json
with open('$ROOT/aws-output.json', encoding='utf-8') as f:
    print(json.load(f).get('$STACK_ID.AgentSecretsArn', ''))
" 2>/dev/null || true)"

if [[ -n "$SECRET_ARN" && ( -n "${ANTHROPIC_API_KEY:-}" || -n "${OPENAI_API_KEY:-}" ) ]]; then
  echo "Pushing secret values to Secrets Manager ($SECRET_ARN)..."
  SECRET_PAYLOAD_FILE="$(mktemp)"
  python3 - <<PY > "$SECRET_PAYLOAD_FILE"
import json
import os

print(json.dumps({
    "LLM_PROVIDER": os.environ.get("LLM_PROVIDER", ""),
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", ""),
    "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
    "OPENAI_MODEL": os.environ.get("OPENAI_MODEL", ""),
    "LANGCHAIN_API_KEY": os.environ.get("LANGCHAIN_API_KEY", ""),
    "LANGCHAIN_TRACING_V2": os.environ.get("LANGCHAIN_TRACING_V2", ""),
    "LANGCHAIN_PROJECT": os.environ.get("LANGCHAIN_PROJECT", ""),
    "API_KEY": os.environ.get("API_KEY", ""),
}))
PY
  aws secretsmanager put-secret-value \
    --secret-id "$SECRET_ARN" \
    --secret-string "file://$SECRET_PAYLOAD_FILE" >/dev/null
  rm -f "$SECRET_PAYLOAD_FILE"
  echo "Secret values updated."
  echo "Note: warm Lambda instances keep whatever they cached at cold start —"
  echo "new values take effect on the next cold start, not immediately."
else
  echo "Skipping Secrets Manager update (no ANTHROPIC_API_KEY/OPENAI_API_KEY in environment/.env)."
  echo "Set manually: aws secretsmanager put-secret-value --secret-id <AgentSecretsArn> --secret-string '{...}'"
fi

echo ""
echo "After deploy ($STACK_ID, stage=$REEVALUATE_STAGE):"
echo "  1. Point any client at the ApiUrl output (LangGraph Platform API — same shapes as LangGraph Cloud)"
echo "  2. Run scripts/smoke_test_aws.sh against it"
