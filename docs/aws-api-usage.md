# AWS API — Trigger, Payload & Output Reference

This documents the live AWS deployment (`ReevaluatePreconditionsStack`, account
`828351637694`, `us-east-2`). It's a Lambda Function URL running a FastAPI app
that speaks a subset of the **LangGraph Platform REST API** — the same
protocol `langgraph_sdk.get_client()` talks to, whether the backend is
LangChain's hosted LangGraph Cloud or (as here) our own AWS deployment. The
env var names in `.env`/`test_cloud.py` (`LANGGRAPH_URL`, `LANGCHAIN_API_KEY`)
are just leftovers from that shared client — nothing here talks to LangSmith
or LangGraph Cloud.

## Base URL & Auth

```
Base URL:    https://h45bdjqgppf4dvw4e5ox6a2pb40leqlr.lambda-url.us-east-2.on.aws
Auth header: x-api-key: <YOUR_AWS_API_KEY>
Assistant:   reevaluate-preconditions
```

Every request needs the `x-api-key` header **except** `GET /health` (see
`api/main.py:api_key_middleware`). Missing/wrong key → `401 {"detail": "Invalid API key"}`.

```bash
curl "$BASE_URL/health"
# {"status":"ok"}
```

## Request payload (same for every trigger method)

Both fields are **raw JSON encoded as strings** (not nested objects) — the
agent parses them itself in STEP_00.

```json
{
  "assistant_id": "reevaluate-preconditions",
  "input": {
    "predicted_conditions_json": "<JSON string — predicted-conditions output containing document_requests>",
    "updated_manifest_json": "<JSON string — manifest with documents[] + extracted metadata>"
  }
}
```

- `predicted_conditions_json` → parsed by `parse_predicted_conditions`; needs a top-level (or `final_output`-nested) `document_requests` array. See [README § predicted_conditions_json](../README.md#predicted_conditions_json) for the item shape.
- `updated_manifest_json` → parsed by `parse_manifest`; a bare list of documents, or `{"documents": [...]}` / `{"manifest": [...]}` / `{"items": [...]}`.

Runtime is typically **50–80 seconds** (Lambda timeout is set to 900s, so there's headroom).

## Ways to trigger it

### 1. Synchronous — simplest, recommended for scripts/webhooks

Blocks for the full run and returns the **entire final graph state** directly (not just `final_output`).

```bash
curl -sS -X POST "$BASE_URL/runs/wait" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d @payload.json
```

Response body = the graph's final state dict. The part you want is `.final_output`:

```jsonc
{
  "predicted_conditions_json": "...",   // echoed input
  "updated_manifest_json": "...",       // echoed input
  "document_requests": [ ... ],         // intermediate parsed state
  "manifest_docs": [ ... ],
  "scenario_summary": { ... },
  "current_step": "STEP_02",
  "step_reports": { ... },
  "final_output": {                     // <-- what you actually want
    "scenario_summary": { ... },
    "document_requests": [ ... ],
    "stats": { ... }
  }
}
```

If you want a **thread you can revisit later** (e.g. to fetch state again, or
keep an audit trail), use `POST /threads/{thread_id}/runs/wait` instead —
identical behavior, but scoped to a thread you create first via `POST /threads`.

### 2. Create-thread + background-run + poll — LangGraph SDK-compatible

This is the pattern `langgraph_sdk`'s `client.runs.create()` / `test_cloud.py`
use. Note: on this deployment, "background" runs actually execute **inline,
synchronously**, within the `POST .../runs` request itself (see
`api/services/runs.py` module docstring) — there's no real async worker. The
run record it returns is already terminal by the time the response comes
back; polling still works, it just resolves on the first check.

```bash
# 1. Create a thread
THREAD_ID=$(curl -sS -X POST "$BASE_URL/threads" \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" -d '{}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['thread_id'])")

# 2. Kick off the run (blocks ~50-80s under the hood, but is a single call)
RUN_ID=$(curl -sS -X POST "$BASE_URL/threads/$THREAD_ID/runs" \
  -H "x-api-key: $API_KEY" -H "Content-Type: application/json" \
  -d @payload.json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['run_id'])")

# 3. Poll (will already be terminal on first check)
curl -sS "$BASE_URL/threads/$THREAD_ID/runs/$RUN_ID" -H "x-api-key: $API_KEY"

# 4. Fetch the full, uncapped final state
curl -sS "$BASE_URL/threads/$THREAD_ID/state" -H "x-api-key: $API_KEY"
```

**Step 3 response** (run record):

```jsonc
{
  "run_id": "...",
  "thread_id": "...",
  "assistant_id": "reevaluate-preconditions",
  "status": "success",           // "error" | "success" (no "pending"/"running" by the time you see it)
  "created_at": "...",
  "updated_at": "...",
  "error": null,
  "result": { ... },             // graph state — capped at ~300KB; see note below
  "input_payload": { ... }       // capped at ~300KB, debugging only
}
```

> ⚠️ **`result` and `input_payload` are size-capped (~300KB each)** to stay
> under DynamoDB's 400KB item limit. For a real loan's data this can get
> truncated (you'll see `{"truncated": true, "reason": "..."}` instead of the
> real payload). **Always use step 4 (`GET /threads/{id}/state`) for the real,
> uncapped final state** — it's never capped.

**Step 4 response** (thread state snapshot):

```jsonc
{
  "values": {
    "final_output": { ... },     // <-- what you actually want
    "document_requests": [ ... ],
    "current_step": "STEP_02",
    ...
  },
  "next": [],
  "config": { "configurable": { "thread_id": "..." } },
  "metadata": { ... },
  "created_at": "...",
  "parent_config": { ... },
  "tasks": []
}
```

### 3. Streaming (SSE)

`POST /threads/{thread_id}/runs/stream` (or stateless `POST /runs/stream`) —
returns `text/event-stream` with LangGraph's stream events as they happen
instead of waiting for the whole run. Same request payload as above.

### Python (`langgraph_sdk`) — what `test_cloud.py` actually does

```python
from langgraph_sdk import get_client

client = get_client(
    url="https://h45bdjqgppf4dvw4e5ox6a2pb40leqlr.lambda-url.us-east-2.on.aws",
    api_key="<YOUR_AWS_API_KEY>",
)

thread = await client.threads.create()
run = await client.runs.create(
    thread_id=thread["thread_id"],
    assistant_id="reevaluate-preconditions",
    input={
        "predicted_conditions_json": predicted_json_string,
        "updated_manifest_json": manifest_json_string,
    },
)
run_status = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
assert run_status["status"] == "success"

state = await client.threads.get_state(thread_id=thread["thread_id"])
final_output = state["values"]["final_output"]
```

Or just reuse the repo's own script directly:

```bash
export REEVALUATE_LANGGRAPH_URL="https://h45bdjqgppf4dvw4e5ox6a2pb40leqlr.lambda-url.us-east-2.on.aws"
export LANGCHAIN_API_KEY="<YOUR_AWS_API_KEY>"
python3 test_cloud.py --predicted <predicted.json> --manifest <manifest.json>
```

## Expected output — `final_output` shape

```json
{
  "scenario_summary": {
    "program": "Investor DSCR",
    "purpose": "Purchase",
    "occupancy": "NOO",
    "property": { "property_type": "SFR", "state": "TN" },
    "numbers": { "loan_amount": 168000, "LTV": 80 },
    "credit": { "fico": 800 },
    "borrowers": [{ "name": "Emmanuel Nyarko" }]
  },
  "document_requests": [
    {
      "document_type": "Bank Statement",
      "status": "partially_satisfied",
      "display": {
        "documentation_requirements": [
          "Document sufficient funds to cover down payment, closing costs, prepaid items, and required reserves."
        ],
        "satisfied_requirements": [
          "Obtain the two most recent consecutive months of personal bank statements for all accounts used for funds to close.",
          "Verify statements include account holder name, account number, financial institution name, statement period, and ending balance.",
          "Confirm all pages of each statement are provided, including any transaction detail pages."
        ]
      }
    }
  ],
  "stats": {
    "total_document_requests": 24,
    "by_status": {
      "needed": 22,
      "fully_satisfied": 1,
      "partially_satisfied": 1
    },
    "total_satisfied_specifications": 31,
    "total_remaining_specifications": 186
  }
}
```

| `status` value | Meaning |
|---|---|
| `fully_satisfied` | All `documentation_requirements` moved to `satisfied_requirements` |
| `partially_satisfied` | Some requirements satisfied, some remain |
| `satisfied_but_review_required` | All satisfied but flagged for manual review |
| `needed` | Nothing in the manifest satisfied any requirement |

The engine only **transfers** requirement strings from
`documentation_requirements` → `satisfied_requirements`; it never invents or
edits text. See [README § Satisfaction Rules](../README.md#satisfaction-rules)
for the matching logic.

## Error responses

| Status | Cause |
|---|---|
| `401` | Missing/wrong `x-api-key` |
| `404` | Unknown `thread_id`/`run_id`, or unknown `assistant_id` |
| `422` | Missing `assistant_id`, or malformed body |
| `500` | Unhandled error inside the agent run — check `error` on the run record or CloudWatch logs (`/aws/lambda/ReevaluatePreconditionsStack-AgentFunction4E676656-en54lUWR57eU`, region `us-east-2`) |

## Real example (verified)

Tested end-to-end against a real loan (24 document requests) from the
predicted-conditions engine — see `test_results/kashana/` for the exact
request/response pair used:

```bash
export REEVALUATE_LANGGRAPH_URL="https://h45bdjqgppf4dvw4e5ox6a2pb40leqlr.lambda-url.us-east-2.on.aws"
export LANGCHAIN_API_KEY="<YOUR_AWS_API_KEY>"
python3 test_cloud.py \
  --predicted test_results/kashana/kashana_predicted.json \
  --manifest test_results/kashana/kashana_manifest.json
```

```
Completed in 0.3s
Results: 24 document requests
Satisfied requirements: 31
Remaining requirements: 186
```

Full output: `test_results/kashana/kashana_reevaluated.json`.
