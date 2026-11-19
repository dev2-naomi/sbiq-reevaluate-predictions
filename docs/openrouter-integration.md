# How This App Calls OpenRouter

Ported 2026-11-19 from the predicted-conditions repo's
`docs/openrouter-integration.md` / `PRIMARY_PROVIDER=openrouter` pattern,
adapted to this repo's (much simpler) single-call-site agent. If you're
also looking at the predicted-conditions version of this doc: this repo
has **no fallback chain and no stall-recovery nudge** — see §4 and §6 for
why that matters.

## 1. Where OpenRouter fits in

OpenRouter is used as an **OpenAI-compatible proxy** in front of
Anthropic's Claude models. This agent has three possible LLM providers,
selected by a single env var (already-existing name in this repo, kept
rather than renaming to match predicted-conditions' `PRIMARY_PROVIDER`):

```
LLM_PROVIDER = "openrouter" (default, as of 2026-11-19) | "anthropic" | "openai"
```

- `"openrouter"` → **new default.** Routes through OpenRouter instead of
  hitting Anthropic or OpenAI directly.
- `"anthropic"` → direct Anthropic API (`ChatAnthropic`), model from
  `ANTHROPIC_MODEL` (default `claude-sonnet-4-20250514`). Currently
  invalid (401) per `.env`.
- `"openai"` → direct OpenAI API, model from `OPENAI_MODEL` (default
  `gpt-5.1`). Was the default until 2026-11-19; still works (see earlier
  chat history re: rotating this key), kept as a fallback option.

OpenRouter was adopted for the same reason predicted-conditions adopted
it: a single proxy reachable via a key independent of the direct
Anthropic/OpenAI account keys, which have both had issues (revoked/
invalid) on this project before.

## 2. The one call site

Unlike predicted-conditions (which has two call sites — the main
orchestrator *and* a separate per-document-request satisfaction check in
`tools/merger_tools.py`), this repo has exactly **one** LLM call, in the
main orchestrator (`agent.py`'s `orchestrator_node`, driving STEP_00
through STEP_02). When `LLM_PROVIDER=openrouter`, the client is built
like this (see `agent.py` ~line 88):

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model=os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-5.5"),
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
    max_tokens=int(os.environ.get("OPENROUTER_MAX_TOKENS", "16384")),
    max_retries=0,
    timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "90")),
    temperature=float(os.environ.get("LLM_TEMPERATURE", "0.2")),
    reasoning_effort=os.environ.get("OPENROUTER_REASONING_EFFORT", "low"),
)
```

It's a plain `langchain_openai.ChatOpenAI` client pointed at OpenRouter's
base URL — from the SDK's perspective it's just "OpenAI"; OpenRouter
transparently proxies to Claude underneath.

Tool-calling is bound via `llm.bind_tools(step_tools)` in
`orchestrator_node()` — **plain auto tool-choice, never forced**
(`tool_choice` is never set anywhere in this repo's graph). This sidesteps
predicted-conditions' documented quirk entirely: there's no
`tool_choice="required"` override here to begin with, so there's nothing
for Claude-via-OpenRouter to reject. If forced tool-choice is ever added
later, re-check §4 below first.

## 3. Full env var reference

| Env var | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `"openrouter"` | `"openrouter"` / `"anthropic"` / `"openai"` |
| `OPENROUTER_API_KEY` | — | OpenRouter API key (required if `LLM_PROVIDER=openrouter`) |
| `OPENROUTER_MODEL` | `anthropic/claude-sonnet-5.5` | Model ID requested from OpenRouter |
| `OPENROUTER_MAX_TOKENS` | `16384` | Explicit output-token cap (see §5 — matters a lot right now) |
| `OPENROUTER_REASONING_EFFORT` | `low` | `low`/`medium`/`high` — see §4 |
| `LLM_TIMEOUT_SECONDS` | `90` | Per-call network timeout, shared by all three providers |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature, shared by openrouter/anthropic (non-Opus) |

Note: there is **no** `LLM_MAX_RETRIES` / `LLM_RETRY_COOLDOWN` /
`LLM_RETRY_MAX_BACKOFF` here (unlike predicted-conditions) — see §6.

## 4. Model-specific quirks (inherited from predicted-conditions — re-validate here)

These were discovered the hard way in the sibling predicted-conditions
app. They should still apply (same model, same proxy), but **have not
been independently re-verified against this agent's shorter workflow**:

- **`claude-sonnet-5.5` has *mandatory* reasoning on OpenRouter.** If
  `reasoning_effort` is left unset, OpenRouter defaults it to `"high"`,
  which is slow and notably more expensive. Defaulted to `"low"` here for
  the same reason.
- **Lower reasoning effort increases the chance of the model stopping
  mid-workflow** without finishing its tool-call sequence.
  predicted-conditions added a "stall-recovery nudge" mechanism to make
  `"low"` effort safe there. **This agent has no equivalent mechanism.**
  Its workflow is much shorter (3 steps vs. 9), which should lower the
  risk, but if a run is observed to stop early with a plain-text reply
  instead of continuing its tool calls, that's this exact failure mode —
  bump `OPENROUTER_REASONING_EFFORT` to `medium` as a first mitigation,
  and consider porting the nudge logic from predicted-conditions'
  `agent.py` (`should_continue()` / `nudge_node()`) if it recurs.
- **Claude (via OpenRouter) rejects a forced `tool_choice="required"`
  outright.** Not currently a risk here (§2 — nothing forces it), but
  don't add forced tool-choice for the openrouter path without accounting
  for this.
- Re-validate all of the above if `OPENROUTER_MODEL` is ever swapped to a
  different model family.

## 5. Why requests sometimes fail with a `402`, and the two *different* things that can cause it

**This is the most important section — read this before assuming
OpenRouter is "down" or the key is "wrong" if you see a 402.** There are
two unrelated knobs that both surface as the same-looking
`402 Payment Required` error:

1. **Per-key weekly spend limit** (`$X`/week, configured on the specific
   API key in the OpenRouter dashboard, resets weekly). Queryable via
   `GET https://openrouter.ai/api/v1/key` with that key as the bearer
   token — returns `effective_budget.remaining_usd`.
2. **Organization-wide prepaid credit balance** (actual real money
   purchased for the whole org, independent of any single key's weekly
   allowance). Queryable via `GET https://openrouter.ai/api/v1/credits`
   — returns `total_credits` and `total_usage`.

**A request can be rejected by #2 even when #1 shows plenty of
"remaining" budget.** The 402 error body's `metadata.limit_source` field
tells you which one actually triggered the rejection
(`"openrouter_credits"` = #2, the real org balance).

`max_tokens` left unset defaults to the model's theoretical max output
(65536 for `claude-sonnet-5.5`), which inflates the request's "required
credits" preflight estimate and makes a 402 more likely regardless of
actual usage — hence the explicit `OPENROUTER_MAX_TOKENS` cap (default
`16384`, §3).

### ⚠️ Live status as of 2026-11-19 (when this integration was wired up)

Checked directly against this exact key (shared with predicted-conditions
— see `.env` comment):

| Limit | Status |
|---|---|
| Per-key weekly budget | $50/week, **~$40.73 remaining** — looks healthy |
| Org-wide prepaid credits | $70 total, **~$0.16 remaining** — nearly exhausted |

Verified empirically: a request with `max_tokens=16384` (this repo's
default) got **402** (`"can only afford 16121"` tokens); the exact same
request with `max_tokens=16000` succeeded, actual cost `$0.000072`. The
margin between "works" and "402" is currently razor-thin (~260 tokens)
and will keep shrinking with any usage from **either app**, since the
org balance is shared. Real reevaluate-preconditions runs may
intermittently 402 until the org tops up credits at
<https://openrouter.ai/settings/credits> — this is the thing most worth
flagging to whoever manages the OpenRouter workspace.

## 6. Retry/fallback behavior — **none** (difference from predicted-conditions)

predicted-conditions treats `402`/`429`/`500`/`502`/`503`/`529` as
transient and retries with backoff, with an OpenAI fallback tier. **This
agent has no such mechanism** — `orchestrator_node()` calls
`llm_with_tools.invoke(injected)` directly with no wrapper. Any error
(402 included) propagates straight up and fails the run. Given §5's
current credit situation, this means a 402 right now is **not**
self-healing here — it just fails. Worth porting a minimal retry wrapper
from predicted-conditions' `_invoke_with_retry` if 402s become frequent
in practice, rather than assuming they'll resolve on their own.

## 7. How to actually switch/add a model

1. Update `OPENROUTER_MODEL`.
2. Re-check §4's quirks against the new model (reasoning-effort support,
   tool-calling behavior, context window size).
3. Re-tune `OPENROUTER_MAX_TOKENS` / `OPENROUTER_REASONING_EFFORT` if the
   new model's pricing or output-length characteristics differ
   meaningfully from `claude-sonnet-5.5`.
4. If deploying: push the new value via `aws secretsmanager
   put-secret-value` against `AgentSecretsArn` (see
   `docs/aws-api-usage.md`), then force a new Lambda execution
   environment (touch any env var via `aws lambda
   update-function-configuration` — `api/secrets.py`'s `load_secrets()`
   only hydrates `os.environ` once per cold start).
