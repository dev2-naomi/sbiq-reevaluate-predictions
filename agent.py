"""
agent.py — Reevaluate Preconditions orchestrator.

Takes predicted-conditions output + updated manifest as input.
Checks each document request's specifications against the manifest.
If a spec is satisfied, transfers it to satisfied_specifications.

Entry point: ./agent.py:agent
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal
from typing_extensions import NotRequired

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

try:
    from langchain_openai import ChatOpenAI
except ImportError:  # pragma: no cover - optional dependency
    ChatOpenAI = None  # type: ignore[assignment,misc]
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt.tool_node import ToolNode
from typing_extensions import TypedDict

from step_loader import load_system_prompt, resolve_plan_for_step, resolve_tools_for_step
from tools import ALL_TOOLS


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------

def _merge_dicts(old: dict | None, new: dict | None) -> dict:
    if old is None:
        old = {}
    if new is None:
        return old
    merged = dict(old)
    for k, v in new.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _merge_dicts(merged[k], v)
        else:
            merged[k] = v
    return merged


def _last_value(old: Any, new: Any) -> Any:
    return new


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ReevaluateState(TypedDict, total=False):
    # Inputs
    predicted_conditions_json: str
    updated_manifest_json: str
    env: str

    # Messages
    messages: Annotated[list[BaseMessage], add_messages]

    # Internal
    document_requests: Annotated[NotRequired[list], _last_value]
    manifest_docs: Annotated[NotRequired[list], _last_value]
    scenario_summary: Annotated[NotRequired[dict], _merge_dicts]
    current_step: Annotated[NotRequired[str], _last_value]
    step_reports: Annotated[NotRequired[dict], _merge_dicts]
    final_output: Annotated[NotRequired[dict], _last_value]


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

# LLM_PROVIDER selects which backend to use ("openrouter" | "anthropic" |
# "openai"). Defaults to "openrouter" — ported from the predicted-conditions
# repo's PRIMARY_PROVIDER=openrouter support (see
# docs/openrouter-integration.md and that repo's agent.py ~2026-11-07/11-19
# history). OpenRouter is an OpenAI-compatible proxy in front of Claude (or
# other models), reached via a key that's independent of the direct
# Anthropic/OpenAI account keys — useful since both of those have had
# issues (revoked/invalid) in the past on this project too.
_PROVIDER = os.environ.get("LLM_PROVIDER", "openrouter").lower()
_SYSTEM_PROMPT = load_system_prompt()

# Per-request network timeout (seconds) and sampling temperature, shared by
# whichever client is constructed below. Same defaults as
# predicted-conditions (docs/openrouter-integration.md §3).
_LLM_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))
_LLM_TEMPERATURE = os.environ.get("LLM_TEMPERATURE", "0.2")

if _PROVIDER == "openrouter":
    if ChatOpenAI is None:
        raise RuntimeError(
            "LLM_PROVIDER=openrouter but langchain-openai is not installed. "
            "Run `pip install langchain-openai`."
        )
    _MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-5.5")
    _or_kwargs: dict = {
        "model": _MODEL,
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": os.environ.get("OPENROUTER_API_KEY"),
        # Explicit cap — left unset, OpenRouter defaults to the model's
        # theoretical max output (65536 for claude-sonnet-5.5), which
        # inflates the request's "required credits" preflight estimate and
        # can trigger a 402 regardless of actual remaining balance. See
        # docs/openrouter-integration.md §5 for the full 402 deep-dive —
        # as of 2026-11-19 the shared org credit balance is nearly
        # exhausted, so this cap matters more than usual right now.
        "max_tokens": int(os.environ.get("OPENROUTER_MAX_TOKENS", "16384")),
        "max_retries": 0,
        "timeout": _LLM_TIMEOUT_SECONDS,
        "temperature": float(_LLM_TEMPERATURE),
    }
    # claude-sonnet-5.5 has MANDATORY reasoning on OpenRouter — if
    # reasoning_effort is left unset, OpenRouter defaults it to "high",
    # which is both slow and expensive. Default "low" per
    # predicted-conditions' measured cost/reliability tradeoff (see
    # docs/openrouter-integration.md §4). NOTE: that repo also had to add a
    # stall-recovery nudge mechanism because "low" effort occasionally lets
    # the model stop mid-workflow without finishing its tool-call sequence.
    # This agent has no equivalent nudge yet — its workflow is much shorter
    # (STEP_00-02 vs. STEP_00-09), which lowers the risk, but has not been
    # load-tested here. Re-validate if stalls are observed.
    _or_effort = os.environ.get("OPENROUTER_REASONING_EFFORT", "low")
    if _or_effort:
        _or_kwargs["reasoning_effort"] = _or_effort
    _llm = ChatOpenAI(**_or_kwargs)
elif _PROVIDER == "openai":
    if ChatOpenAI is None:
        raise RuntimeError(
            "LLM_PROVIDER=openai but langchain-openai is not installed. "
            "Run `pip install langchain-openai`."
        )
    _MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.1")
    _llm = ChatOpenAI(model=_MODEL, max_tokens=16384, timeout=_LLM_TIMEOUT_SECONDS)
else:
    _MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    _llm_kwargs: dict = {"model": _MODEL, "max_tokens": 16384, "timeout": _LLM_TIMEOUT_SECONDS}
    if "opus" in _MODEL:
        _llm_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8192}
    else:
        _llm_kwargs["temperature"] = float(_LLM_TEMPERATURE)
    _llm = ChatAnthropic(**_llm_kwargs)

# NOTE on tool_choice: orchestrator_node() below binds tools with plain
# llm.bind_tools(step_tools) (auto tool choice) — it never forces
# tool_choice="required". That's the one OpenAI-primary quirk documented in
# predicted-conditions (forced tool_choice is OpenAI-only there, and Claude
# via OpenRouter rejects it outright) that does NOT need a special case
# here, since this agent never forces tool_choice for any provider.


# ---------------------------------------------------------------------------
# Default prompt
# ---------------------------------------------------------------------------

_DEFAULT_INITIAL_PROMPT = (
    "Execute the Reevaluate Preconditions workflow: STEP_00 → STEP_01 → STEP_02.\n\n"
    "STEP_00: Call parse_predicted_conditions, then parse_manifest, then save_step_report.\n"
    "STEP_01: Review each document_request's specifications against the manifest_docs.\n"
    "  For each doc request, find matching manifest docs by type. Then for each spec,\n"
    "  check if the manifest data satisfies it. Call evaluate_specifications with your findings.\n"
    "  Then save_step_report.\n"
    "STEP_02: Call generate_final_output, then save_step_report.\n\n"
    "IMPORTANT: Only TRANSFER existing specs to satisfied — never create new ones."
)


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

_STEP_SAVE_PATTERN = "Step report saved for "


def _summarize_completed_steps(
    messages: list[BaseMessage],
    current_step: str | None,
    step_reports: dict,
) -> list[BaseMessage]:
    if not messages or not current_step or not step_reports:
        return messages

    boundary_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, ToolMessage):
            content = msg.content if isinstance(msg.content, str) else ""
            if _STEP_SAVE_PATTERN in content:
                after = content.split(_STEP_SAVE_PATTERN, 1)[1]
                step_id = after.split(".")[0].strip()
                if step_id != current_step:
                    boundary_idx = i
                    break

    if boundary_idx < 3:
        return messages

    summary_lines = ["[COMPLETED STEPS SUMMARY]"]
    for step_id, report in sorted(step_reports.items()):
        summary_lines.append(f"- {step_id}: {report.get('summary', 'Done')}")

    first_human = None
    for msg in messages:
        if isinstance(msg, HumanMessage):
            first_human = msg
            break

    current_msgs = messages[boundary_idx + 1:]

    result: list[BaseMessage] = []
    if first_human:
        result.append(first_human)
    result.append(SystemMessage(content="\n".join(summary_lines)))
    result.extend(current_msgs)
    return result


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def orchestrator_node(state: ReevaluateState) -> dict:
    current_step = state.get("current_step") or "STEP_00"
    step_tools = resolve_tools_for_step(state)
    llm_with_tools = _llm.bind_tools(step_tools)

    messages: list[BaseMessage] = list(state.get("messages", []))
    step_reports = state.get("step_reports", {})

    if not any(isinstance(m, HumanMessage) for m in messages):
        messages = [HumanMessage(content=_DEFAULT_INITIAL_PROMPT)] + messages

    messages = _summarize_completed_steps(messages, current_step, step_reports)

    plan = resolve_plan_for_step(state)
    system_parts: list[str] = [_SYSTEM_PROMPT]
    if plan:
        system_parts.append(f"[CURRENT STEP PLAN]\n\n{plan}")

    non_system: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            system_parts.append(msg.content if isinstance(msg.content, str) else str(msg.content))
        else:
            non_system.append(msg)

    injected = [SystemMessage(content="\n\n---\n\n".join(system_parts))] + non_system
    response: AIMessage = llm_with_tools.invoke(injected)
    return {"messages": [response], "current_step": current_step}


def should_continue(state: ReevaluateState) -> Literal["tools", "end"]:
    messages = state.get("messages", [])
    if not messages:
        return "end"
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "end"


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

_tool_node = ToolNode(ALL_TOOLS)


def _build_graph():
    builder = StateGraph(ReevaluateState)
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("tools", _tool_node)

    builder.set_entry_point("orchestrator")
    builder.add_conditional_edges("orchestrator", should_continue, {"tools": "tools", "end": END})
    builder.add_edge("tools", "orchestrator")
    return builder


def _compile_agent(run_config: dict | None = None, *, checkpointer: Any | None = None):
    """Graph factory used by the AWS API shim (api/registry.py).

    ``run_config`` is accepted for interface parity with other agents in the
    fleet (e.g. LG-docsOrch's factory signature) but is unused here — this
    agent has no per-run configuration beyond the checkpointer/thread_id,
    which LangGraph itself threads through via ``config`` at invoke time.
    """
    return _build_graph().compile(checkpointer=checkpointer).with_config({"recursion_limit": 50})


# Module-level compiled graph — used by `langgraph dev`/LangGraph Cloud
# (langgraph.json points at "./agent.py:agent") and by test_pipeline.py.
# LangGraph Cloud manages its own checkpointer; this compiled instance never
# needs one directly.
agent = _build_graph().compile().with_config({"recursion_limit": 50})
