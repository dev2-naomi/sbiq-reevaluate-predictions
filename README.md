# Reevaluate Preconditions

A reevaluation engine that takes the output of the [Predicted Conditions](https://github.com/dev2-naomi/sbiq-predicted-conditions) pipeline and an updated document manifest, then determines which predicted documentation requirements are now satisfied by the manifest. Satisfied requirements are transferred from `display.documentation_requirements` to `display.satisfied_requirements`. Built on [LangGraph](https://langchain-ai.github.io/langgraph/) with a single ReAct agent loop.

## How It Works

The system operates as a **3-step sequential pipeline** orchestrated by a single LLM agent. Each step has scoped tools and a plan file.

```
STEP_00   Input Parser              ─ Parse predicted conditions + manifest into structured state
STEP_01   Specification Evaluator   ─ Match document types, check requirements against manifest fields
STEP_02   Output Generator          ─ Assemble final output with updated statuses and stats
```

### Key Design Decisions

- **Deterministic matching first**: Document type aliases and keyword-to-field matching run before LLM evaluation. The LLM supplements by reasoning over borderline cases where field data doesn't directly map to a requirement.

- **Transfer-only logic**: The system never creates new requirements or modifies existing text. It only moves items from `documentation_requirements` to `satisfied_requirements`.

- **Blanket alias resolution**: When a manifest document matches via a blanket alias (e.g., "Borrower Certification" ↔ "Borrowers Authorization"), all requirements for that document request are auto-satisfied without field-level checking.

- **Dynamic tool scoping**: Before each LLM invocation, only the tools for the current step are bound. This keeps token cost low and prevents out-of-scope tool calls.

- **Message summarization**: Completed steps are compressed into a summary before each LLM call, keeping only the current step's messages in full detail.

## Inputs

The agent accepts two primary inputs:

| Input | Format | Description |
|-------|--------|-------------|
| `predicted_conditions_json` | JSON string | Output from predicted-conditions (`final_state.json` or `*_output.json`) containing `document_requests` |
| `updated_manifest_json` | JSON string | Document manifest with `documents[]` containing classified documents and extracted metadata |

### `predicted_conditions_json`

The full output from predicted-conditions. Must contain `document_requests` where each request has:

```json
{
  "document_type": "Bank Statement",
  "status": "needed",
  "specifications": ["..."],
  "display": {
    "document_heading": "Bank Statement",
    "documentation_requirements": [
      "Obtain the two most recent consecutive months of personal bank statements for all accounts used for funds to close.",
      "Verify statements include account holder name, account number, financial institution name, statement period, and ending balance.",
      "Confirm all pages of each statement are provided, including any transaction detail pages.",
      "Document sufficient funds to cover down payment, closing costs, prepaid items, and required reserves."
    ],
    "reason_for_requirement": ["..."],
    "review_notes": ["..."],
    "satisfied_requirements": []
  }
}
```

### `updated_manifest_json`

A document manifest containing available documents with extracted metadata:

```json
{
  "documents": [
    {
      "category": {
        "category_name": "Bank Statements"
      },
      "metadata": {
        "account_holder": "Emmanuel Nyarko",
        "account_number": "****8700",
        "institution_name": "Bank of America",
        "statement_period": "01/01/2025 - 01/28/2025",
        "ending_balance": "$12,646.56"
      }
    }
  ]
}
```

### Cloud API

**Base URL**: `https://sbiq-reevaluate-predictions-8af10a300ebf5186bf271ec255a3f2cb.us.langgraph.app`

Both inputs are passed as raw JSON strings. The agent auto-generates its own instruction prompt and defaults to `STEP_00`, so callers only need to send data.

```json
POST /threads/{thread_id}/runs

{
  "assistant_id": "reevaluate-preconditions",
  "input": {
    "predicted_conditions_json": "<raw predicted-conditions output JSON string>",
    "updated_manifest_json": "<raw document manifest JSON string>"
  }
}
```

Typical runtime is 50-80 seconds. The final output is available in the thread state under `final_output`.

```python
from langgraph_sdk import get_client

client = get_client(
    url="https://sbiq-reevaluate-predictions-8af10a300ebf5186bf271ec255a3f2cb.us.langgraph.app",
    api_key="<LANGSMITH_API_KEY>",
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

await client.runs.join(thread_id=thread["thread_id"], run_id=run["run_id"])
state = await client.threads.get_state(thread_id=thread["thread_id"])
result = state["values"]["final_output"]
```

### How the Agent Uses the Data

1. **STEP_00** parses both inputs. `parse_predicted_conditions` extracts the `document_requests` array and `scenario_summary`. `parse_manifest` normalizes each document's `category.category_name` into `detected_document_type` and flattens `metadata` into `extracted_fields`.

2. **STEP_01** iterates through each document request, finds matching manifest documents via type aliases (`_DOCTYPE_ALIASES` and `_DOCTYPE_BLANKET_ALIASES`), then checks each `display.documentation_requirements` entry against the manifest's extracted fields using keyword-to-field matching (`SPEC_KEYWORD_TO_FIELDS`). Satisfied requirements are moved to `display.satisfied_requirements`.

3. **STEP_02** assembles the final output JSON with updated statuses and computes aggregate stats.

## Output

The final output mirrors the predicted-conditions structure with requirements evaluated and transferred:

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
    },
    {
      "document_type": "Borrower Certification as to Business Purpose",
      "status": "fully_satisfied",
      "display": {
        "documentation_requirements": [],
        "satisfied_requirements": [
          "Obtain a signed Business Purpose Affidavit from all borrowers on the loan.",
          "Verify the certification confirms the loan is for business or investment purposes, not consumer or personal use.",
          "Confirm the affidavit is dated and properly executed per program requirements."
        ]
      }
    }
  ],
  "stats": {
    "total_document_requests": 19,
    "by_status": {
      "needed": 16,
      "fully_satisfied": 1,
      "partially_satisfied": 2
    },
    "total_satisfied_specifications": 10,
    "total_remaining_specifications": 58
  }
}
```

### Status Values

| Status | Meaning |
|--------|---------|
| `fully_satisfied` | All `documentation_requirements` moved to `satisfied_requirements` |
| `partially_satisfied` | Some requirements satisfied, some remain |
| `satisfied_but_review_required` | All satisfied but flagged for manual review |
| `needed` | No requirements satisfied by the manifest |

### Satisfaction Rules

1. Only **transfers** existing requirements — never creates new ones
2. A requirement is satisfied when the manifest has a matching document type with relevant extracted field data
3. Blanket aliases (e.g., "Borrower Certification" ↔ "Borrowers Authorization") auto-satisfy all requirements for the matched document type
4. Deterministic keyword-to-field matching supplements LLM evaluation for borderline cases

## Project Structure

```
reevaluate-preconditions/
├── agent.py                  # LangGraph StateGraph, orchestrator node, message summarization
├── registry.py               # Auto-generated step→tool mappings from workflow_config.json
├── step_loader.py            # Dynamic tool/plan resolution per step
├── test_pipeline.py          # Local end-to-end test runner
├── test_cloud.py             # Cloud deployment test runner
│
├── plans/                    # Step plan files (injected as system messages)
│   ├── system_prompt.md
│   ├── step_00_input_parser.md
│   ├── step_01_evaluate.md
│   └── step_02_output.md
│
├── tools/
│   ├── __init__.py               # Exports ALL_TOOLS and per-step tool lists
│   ├── general.py                # Cross-step: save_step_report, get_workflow_status
│   ├── parsing_tools.py          # STEP_00: parse predicted conditions + manifest
│   ├── evaluation_tools.py       # STEP_01: evaluate requirements against manifest
│   ├── output_tools.py           # STEP_02: generate final output JSON
│   └── shared/
│       └── manifest_matcher.py   # Document type aliases + keyword-to-field matching
│
├── config/
│   ├── workflow_config.json      # Step definitions, tool assignments
│   └── generate.py               # Generates registry.py from workflow_config.json
│
├── requirements.txt
├── env.example
└── langgraph.json                # LangGraph deployment descriptor
```

## Architecture Details

### State Management

The agent uses a `ReevaluateState` TypedDict with custom reducers:

| Field | Reducer | Description |
|-------|---------|-------------|
| `predicted_conditions_json` | — | Raw predicted-conditions output string (input) |
| `updated_manifest_json` | — | Raw manifest JSON string (input) |
| `messages` | `add_messages` | Append-only message history |
| `document_requests` | `_last_value` | Parsed document requests from predicted-conditions |
| `manifest_docs` | `_last_value` | Normalized manifest documents with extracted fields |
| `scenario_summary` | `_merge_dicts` | Deep-merged scenario context |
| `current_step` | `_last_value` | Auto-advanced by `save_step_report` |
| `step_reports` | `_merge_dicts` | Per-step summaries for summarization |
| `final_output` | `_last_value` | The assembled output JSON |

### Manifest Matching (`tools/shared/manifest_matcher.py`)

The matcher uses two alias systems:

- **`_DOCTYPE_ALIASES`**: Maps predicted document type names to manifest category names (e.g., `"bank statement"` → `["bank statements", "bank statement"]`)
- **`_DOCTYPE_BLANKET_ALIASES`**: Maps document types that should auto-satisfy all requirements when found (e.g., `"borrower certification as to business purpose"` → `["borrowers authorization"]`)

Field matching uses `SPEC_KEYWORD_TO_FIELDS` which maps keywords found in requirements to expected manifest metadata fields (e.g., `"account holder"` → `["account_holder", "account_holder_name"]`).

### Message Summarization

Completed steps are compressed into a compact summary before each LLM invocation. Only the current step's messages are kept in full detail. This prevents context growth across steps.
