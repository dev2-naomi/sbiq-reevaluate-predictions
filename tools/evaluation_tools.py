"""
evaluation_tools.py — STEP_01: Evaluate each document request's display.documentation_requirements
against the manifest. Requirements that the manifest satisfies get moved to
display.satisfied_requirements. No new requirements are created.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from typing_extensions import Annotated

from tools.shared.manifest_matcher import (
    check_spec_against_manifest,
    find_matching_manifest_docs,
    is_blanket_alias,
)


def _as_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        return [val]
    return [val] if val else []


@tool
def view_parsed_data(
    state: Annotated[dict, InjectedState] = None,
) -> dict:
    """
    Return the parsed document_requests and manifest_docs content so you can
    actually read the documentation_requirements text and extracted_fields
    before evaluating.

    Call this first if you haven't seen this data in the conversation yet —
    parse_predicted_conditions/parse_manifest (STEP_00) only emit short
    count/type summaries as their tool output, never the full content. The
    real data lives in graph state, and this is the only tool that surfaces
    it back to you.
    """
    s = state or {}
    document_requests = _as_list(s.get("document_requests", []))
    manifest_docs = _as_list(s.get("manifest_docs", []))

    requests_view = []
    for dr in document_requests:
        display = dr.get("display", {}) or {}
        requests_view.append({
            "document_type": dr.get("document_type"),
            "status": dr.get("status"),
            "documentation_requirements": display.get("documentation_requirements", []),
            "satisfied_requirements": display.get("satisfied_requirements", []),
        })

    manifest_view = [
        {
            "detected_document_type": m.get("detected_document_type"),
            "extracted_fields": m.get("extracted_fields", {}),
        }
        for m in manifest_docs
    ]

    return {
        "document_requests": requests_view,
        "manifest_docs": manifest_view,
    }


@tool
def evaluate_specifications(
    evaluations: list[dict],
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    state: Annotated[dict, InjectedState] = None,
) -> Command:
    """
    Evaluate which documentation_requirements in the display object are satisfied
    by the manifest. Satisfied ones get transferred to display.satisfied_requirements.

    For each document request, look at display.documentation_requirements, check
    them against the manifest, and report which ones (by index) are satisfied.

    This tool pulls document_requests and manifest_docs directly from graph
    state and ALWAYS runs deterministic keyword/alias matching over every
    document request regardless of what you pass in `evaluations`. It is
    always safe to call — even with `evaluations=[]` — because the
    deterministic pass is the primary mechanism and your input only
    supplements borderline cases you're confident about. Never skip calling
    this tool because you're unsure what the exact requirement text is; call
    `view_parsed_data` first if you want to see it, but an empty/partial
    `evaluations` list is a valid, expected call when you can't.

    Args:
        evaluations: List of evaluation results, one per document request
            you have a confident, specific judgment about (may be empty):
            - document_type (str): The document_type being evaluated
            - satisfied_indices (list[int]): 0-based indices of documentation_requirements
              that ARE satisfied by the manifest
            - reasons (list[str]): Why each is satisfied (same length as satisfied_indices)
    """
    s = state or {}
    document_requests = _as_list(s.get("document_requests", []))
    manifest_docs = _as_list(s.get("manifest_docs", []))

    # Index LLM evaluations by document_type
    eval_by_type: dict[str, dict] = {}
    for evaluation in evaluations:
        dt = (evaluation.get("document_type") or "").strip().lower()
        if dt:
            eval_by_type[dt] = evaluation

    total_satisfied = 0
    updated_requests = [dict(dr) for dr in document_requests]

    for idx, dr in enumerate(updated_requests):
        doc_type = (dr.get("document_type") or "").strip()
        doc_type_lower = doc_type.lower()

        display = dr.get("display", {})
        if not display:
            continue

        doc_reqs = _as_list(display.get("documentation_requirements", []))
        existing_satisfied = _as_list(display.get("satisfied_requirements", []))

        if not doc_reqs:
            continue

        # Get LLM evaluation if provided
        evaluation = eval_by_type.get(doc_type_lower, {})
        llm_satisfied_indices = _as_list(evaluation.get("satisfied_indices", []))
        llm_reasons = _as_list(evaluation.get("reasons", []))

        # Find manifest matches
        manifest_matches = find_matching_manifest_docs(doc_type, manifest_docs)

        satisfied_idx_set: set[int] = set()
        new_satisfied: list[str] = []

        # First: apply LLM's explicit evaluations
        for i, req_idx in enumerate(llm_satisfied_indices):
            if req_idx < 0 or req_idx >= len(doc_reqs):
                continue
            satisfied_idx_set.add(req_idx)
            new_satisfied.append(doc_reqs[req_idx])

        # Second: deterministic pass for remaining requirements
        for req_idx, req_text in enumerate(doc_reqs):
            if req_idx in satisfied_idx_set:
                continue
            for mdoc in manifest_matches:
                extracted = mdoc.get("extracted_fields", {})

                # Blanket alias: all requirements satisfied
                matched_name = mdoc.get("detected_document_type", "")
                if is_blanket_alias(doc_type, matched_name):
                    satisfied_idx_set.add(req_idx)
                    new_satisfied.append(req_text)
                    break

                if not extracted:
                    continue

                reason = check_spec_against_manifest(req_text, extracted)
                if reason:
                    satisfied_idx_set.add(req_idx)
                    new_satisfied.append(req_text)
                    break

        # Transfer: move satisfied from documentation_requirements to satisfied_requirements
        remaining_reqs = [doc_reqs[i] for i in range(len(doc_reqs)) if i not in satisfied_idx_set]

        # Deduplicate against existing satisfied_requirements
        existing_lower = {s.strip().lower() for s in existing_satisfied}
        for sat in new_satisfied:
            if sat.strip().lower() not in existing_lower:
                existing_satisfied.append(sat)
                existing_lower.add(sat.strip().lower())
                total_satisfied += 1

        # Update the display object
        display = dict(display)
        display["documentation_requirements"] = remaining_reqs
        display["satisfied_requirements"] = existing_satisfied
        dr["display"] = display

        # Update top-level status
        if not remaining_reqs and existing_satisfied:
            dr["status"] = "fully_satisfied"
        elif existing_satisfied and remaining_reqs:
            dr["status"] = "partially_satisfied"
        elif manifest_matches and not existing_satisfied:
            dr["status"] = "satisfied_but_review_required"

    msg = (
        f"Evaluated {len(updated_requests)} document requests. "
        f"Transferred {total_satisfied} documentation_requirements to satisfied_requirements."
    )

    return Command(update={
        "document_requests": updated_requests,
        "messages": [ToolMessage(msg, tool_call_id=tool_call_id)],
    })
