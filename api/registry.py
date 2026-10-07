"""Maps LangGraph Platform assistant_id values to graph factories.

reevaluate-preconditions is a single-graph agent, so this registry has
exactly one entry — matching the assistant_id used today against the
LangGraph Cloud deployment (see README.md's Cloud API section).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from agent import _compile_agent

GraphFactory = Callable[..., Any]

ASSISTANTS: dict[str, dict[str, str]] = {
    "reevaluate-preconditions": {
        "assistant_id": "reevaluate-preconditions",
        "graph_id": "reevaluate-preconditions",
        "name": "Reevaluate Preconditions",
        "description": "Reevaluates predicted document requirements against an updated manifest.",
    },
}

FACTORIES: dict[str, GraphFactory] = {
    "reevaluate-preconditions": _compile_agent,
}


def list_assistants() -> list[dict[str, str]]:
    return list(ASSISTANTS.values())


def get_factory(assistant_id: str) -> GraphFactory:
    if assistant_id not in FACTORIES:
        raise KeyError(f"Unknown assistant_id: {assistant_id}")
    return FACTORIES[assistant_id]


def build_graph(
    assistant_id: str,
    run_config: RunnableConfig | dict[str, Any],
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    factory = get_factory(assistant_id)
    return factory(run_config, checkpointer=checkpointer)
