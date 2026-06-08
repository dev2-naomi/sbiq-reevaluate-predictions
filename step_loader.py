"""
step_loader.py — Load plan files and resolve tools per step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from registry import GENERAL_TOOL_NAMES, get_current_step, get_step_plan_file, get_step_tools

PLANS_DIR = Path(__file__).parent / "plans"


def load_plan_content(step_id: str) -> str | None:
    plan_file = get_step_plan_file(step_id)
    if not plan_file:
        return None
    path = PLANS_DIR / plan_file
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def load_system_prompt() -> str:
    path = PLANS_DIR / "system_prompt.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "You are the SBIQ AI Reevaluate Preconditions Orchestrator."


def _import_all_tools() -> dict[str, Any]:
    from tools import ALL_TOOLS
    return {t.name: t for t in ALL_TOOLS}


_TOOL_REGISTRY: dict[str, Any] | None = None


def get_tool_registry() -> dict[str, Any]:
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is None:
        _TOOL_REGISTRY = _import_all_tools()
    return _TOOL_REGISTRY


def resolve_tools_for_step(state: dict) -> list[Any]:
    registry = get_tool_registry()
    current_step = get_current_step(state) or "STEP_00"
    general_tools = [registry[name] for name in GENERAL_TOOL_NAMES if name in registry]

    step_tool_names = get_step_tools(current_step)
    step_tools = [registry[name] for name in step_tool_names if name in registry]

    seen: set[str] = set()
    result: list[Any] = []
    for tool in general_tools + step_tools:
        if tool.name not in seen:
            seen.add(tool.name)
            result.append(tool)
    return result


def resolve_plan_for_step(state: dict) -> str | None:
    current_step = get_current_step(state)
    if not current_step:
        return None
    return load_plan_content(current_step)
