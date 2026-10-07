"""Graph run execution helpers.

reevaluate-preconditions runs finish in ~50-80s (README) — well under
Lambda's 900s ceiling — so unlike LG-docsOrch's migration this deliberately
skips the ECS Fargate worker entirely (per the migration playbook §4.6:
"if your agent always finishes well under 15 minutes, skip the Fargate
worker and run everything inline in the Lambda").

`POST /threads/{id}/runs` ("background run") therefore executes the graph
*synchronously, inline, within the same request* rather than truly
dispatching elsewhere — kept as a distinct endpoint (rather than requiring
every client to switch to /runs/wait) purely for LangGraph SDK client
compatibility (`client.runs.create` + poll, as used by test_cloud.py); the
POST response already carries the terminal status, so a client's first
poll of GET /threads/{id}/runs/{run_id} immediately sees it.

NOTE on why there's no real async dispatch here (Lambda self-invoke /
Fargate / background thread): this stack runs the container via the AWS
Lambda Web Adapter (Dockerfile CMD starts a real HTTP server; the adapter
extension translates every Lambda invocation — including an async
self-invoke `Event` payload — into an HTTP request against that server).
A generic (non-HTTP-shaped) self-invoke payload gets converted into a
`POST /events` request the app doesn't recognize, so a Python-level
`handler()`/Mangum interception (as LG-docsOrch's Fargate-first stack has)
never actually runs — verified empirically against this exact deployment.
Given the short runtime, inline synchronous execution sidesteps that
mismatch entirely rather than working around it.

There is also no Command(resume=...)/HITL handling here — this agent has
no interrupt() call anywhere (see agent.py), unlike LG-docsOrch's
review_flags tool.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from checkpointer import get_checkpointer

from api.platform.sse import (
    _json_safe,
    format_end_event,
    format_error_event,
    format_metadata_event,
    format_sse_event,
    normalize_stream_modes,
    serialize_stream_payload,
    stream_event_name,
)
from api.registry import build_graph
from api.thread_store import get_thread_store

# Margin under DynamoDB's hard 400KB item cap for a run record's `result`
# field. reevaluate-preconditions' final_output (see README's example) is
# comfortably smaller than this for realistic inputs, but the guard is kept
# for defense-in-depth — GET /threads/{id}/state remains the uncapped
# source of truth either way.
_MAX_RUN_RESULT_BYTES = 300_000


def _capped_result(result: Any) -> Any:
    try:
        size = len(json.dumps(result, default=str))
    except Exception:  # noqa: BLE001 - if we can't even measure it, truncate
        size = _MAX_RUN_RESULT_BYTES + 1
    if size <= _MAX_RUN_RESULT_BYTES:
        return result
    return {
        "truncated": True,
        "reason": (
            f"Final result ({size} bytes) exceeds the run record's storage "
            f"limit ({_MAX_RUN_RESULT_BYTES} bytes) and was omitted. Fetch "
            "GET /threads/{thread_id}/state for the full final state instead "
            "— it has no size cap."
        ),
    }


def _merge_config(run_body: dict[str, Any], thread_id: str | None) -> dict[str, Any]:
    config: dict[str, Any] = dict(run_body.get("config") or {})
    configurable = dict(config.get("configurable") or {})
    if thread_id:
        configurable["thread_id"] = thread_id
    config["configurable"] = configurable
    return config


def _checkpointer_for_run(thread_id: str | None):
    if thread_id:
        return get_checkpointer()
    return None


def _record_thread_run(thread_id: str | None, assistant_id: str, run_body: dict[str, Any]) -> None:
    if not thread_id:
        return
    configurable = (run_body.get("config") or {}).get("configurable") or {}
    get_thread_store().record_run(
        thread_id,
        assistant_id=assistant_id,
        metadata={k: configurable[k] for k in ("env",) if configurable.get(k) is not None},
    )


def _run_payload(run_body: dict[str, Any]) -> Any:
    return run_body.get("input") or {}


def invoke_run(
    assistant_id: str,
    run_body: dict[str, Any],
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    config = _merge_config(run_body, thread_id)
    _record_thread_run(thread_id, assistant_id, run_body)
    graph = build_graph(assistant_id, config, checkpointer=_checkpointer_for_run(thread_id))
    return graph.invoke(_run_payload(run_body), config=config)


def stream_run(
    assistant_id: str,
    run_body: dict[str, Any],
    *,
    thread_id: str | None = None,
) -> Iterator[str]:
    config = _merge_config(run_body, thread_id)
    _record_thread_run(thread_id, assistant_id, run_body)
    graph = build_graph(assistant_id, config, checkpointer=_checkpointer_for_run(thread_id))
    payload = _run_payload(run_body)
    stream_modes = normalize_stream_modes(run_body)
    run_id = str(uuid.uuid4())

    yield format_metadata_event(run_id)

    try:
        for event in graph.stream(
            payload,
            config=config,
            stream_mode=stream_modes,
        ):
            if isinstance(event, tuple) and len(event) == 2:
                mode, event_payload = event
                yield format_sse_event(
                    stream_event_name(mode),
                    serialize_stream_payload(mode, event_payload),
                )
            else:
                yield format_sse_event("updates", serialize_stream_payload("updates", event))
    except Exception as exc:
        yield format_error_event(str(exc))
        raise
    finally:
        yield format_end_event()


# ── Background runs (executed synchronously inline — see module docstring) ─
#
# `create_background_run` blocks for the run's full duration (~50-80s) and
# returns the already-terminal run record. A polling client (e.g.
# test_cloud.py / the LangGraph SDK) still works unmodified: its first call
# to GET /threads/{id}/runs/{run_id} just immediately sees a terminal
# status instead of "pending".


def execute_background_run(
    thread_id: str,
    run_id: str,
    assistant_id: str,
    run_body: dict[str, Any],
) -> dict[str, Any]:
    """Actually execute a background run to completion (or failure).

    Deliberately never raises: whichever compute calls this (Lambda
    self-invoke or a local thread) needs the run record to reliably reach a
    terminal status so a polling client never sees a run stuck on "running"
    forever just because the worker process itself crashed calling this.
    """
    store = get_thread_store()
    store.update_run(run_id, status="running")
    config = _merge_config(run_body, thread_id)
    _record_thread_run(thread_id, assistant_id, run_body)
    try:
        graph = build_graph(assistant_id, config, checkpointer=_checkpointer_for_run(thread_id))
        result = graph.invoke(_run_payload(run_body), config=config)
    except Exception as exc:  # noqa: BLE001 - always record *a* terminal status
        store.update_run(run_id, status="error", error=str(exc))
        return {"status": "error", "error": str(exc)}

    safe_result = _capped_result(_json_safe(result))
    try:
        store.update_run(run_id, status="success", result=safe_result)
    except Exception as exc:  # noqa: BLE001 - never let a persist failure
        # strand the run on "running" forever.
        try:
            store.update_run(
                run_id,
                status="success",
                result=str(safe_result),
                error=f"Result not fully serializable: {exc}",
            )
        except Exception as exc2:  # noqa: BLE001
            store.update_run(run_id, status="error", error=f"Failed to persist run result: {exc2}")
            return {"status": "error", "error": f"Failed to persist run result: {exc2}"}
        return {"status": "success", "result": str(safe_result)}
    return {"status": "success", "result": safe_result}


def create_background_run(
    thread_id: str,
    assistant_id: str,
    run_body: dict[str, Any],
) -> dict[str, Any]:
    """Create a run record and execute it inline (see module docstring for
    why this is synchronous rather than dispatched to a worker)."""
    store = get_thread_store()
    run = store.create_run(thread_id, assistant_id=assistant_id, run_body=run_body)
    run_id = run["run_id"]

    execute_background_run(thread_id, run_id, assistant_id, run_body)

    return store.get_run(run_id) or run


def get_background_run(run_id: str) -> dict[str, Any] | None:
    return get_thread_store().get_run(run_id)
