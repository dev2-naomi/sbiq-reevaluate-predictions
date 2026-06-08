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

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
_SYSTEM_PROMPT = load_system_prompt()

_llm_kwargs: dict = {"model": _MODEL, "max_tokens": 16384}
if "opus" in _MODEL:
    _llm_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8192}

_llm = ChatAnthropic(**_llm_kwargs)


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

_builder = StateGraph(ReevaluateState)
_builder.add_node("orchestrator", orchestrator_node)
_builder.add_node("tools", _tool_node)

_builder.set_entry_point("orchestrator")
_builder.add_conditional_edges("orchestrator", should_continue, {"tools": "tools", "end": END})
_builder.add_edge("tools", "orchestrator")

agent = _builder.compile().with_config({"recursion_limit": 50})
