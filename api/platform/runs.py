"""LangGraph Platform-compatible run endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.platform.models import RunCreate
from api.services.runs import create_background_run, get_background_run, invoke_run, stream_run

router = APIRouter(tags=["runs"])


@router.post("/runs/wait")
async def runs_wait(body: RunCreate):
    if not body.assistant_id:
        raise HTTPException(status_code=422, detail="assistant_id is required")
    configurable = (body.config or {}).get("configurable") or {}
    if configurable.get("thread_id"):
        raise HTTPException(
            status_code=422,
            detail="thread_id in config.configurable is not allowed on threadless runs",
        )
    try:
        return invoke_run(body.assistant_id, body.model_dump())
    except KeyError as exc:
        detail = exc.args[0] if exc.args else str(exc)
        if str(detail).startswith("Unknown assistant_id"):
            raise HTTPException(status_code=404, detail=str(detail)) from exc
        raise HTTPException(status_code=500, detail=str(detail)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/runs/stream")
async def runs_stream(body: RunCreate):
    if not body.assistant_id:
        raise HTTPException(status_code=422, detail="assistant_id is required")
    configurable = (body.config or {}).get("configurable") or {}
    if configurable.get("thread_id"):
        raise HTTPException(
            status_code=422,
            detail="thread_id in config.configurable is not allowed on threadless runs",
        )
    try:
        return StreamingResponse(
            stream_run(body.assistant_id, body.model_dump()),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/runs/wait")
async def thread_runs_wait(thread_id: str, body: RunCreate):
    if not body.assistant_id:
        raise HTTPException(status_code=422, detail="assistant_id is required")
    try:
        return invoke_run(body.assistant_id, body.model_dump(), thread_id=thread_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/runs/stream")
async def thread_runs_stream(thread_id: str, body: RunCreate):
    if not body.assistant_id:
        raise HTTPException(status_code=422, detail="assistant_id is required")
    try:
        return StreamingResponse(
            stream_run(body.assistant_id, body.model_dump(), thread_id=thread_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/runs")
async def thread_runs_create(thread_id: str, body: RunCreate):
    """Create a background run and return immediately (status "pending").

    reevaluate-preconditions runs finish in ~50-80s (README), well under
    Lambda's 900s ceiling, so this dispatches via a Lambda self-invoke (or a
    local Python thread when running outside Lambda) rather than an ECS
    Fargate task — see docs/aws-migration-notes.md and the migration
    playbook §4.6 ("if your agent always finishes well under 15 minutes,
    skip the Fargate worker"). Kept as a background-run endpoint (rather
    than requiring every client to switch to /runs/wait) purely for
    LangGraph SDK client compatibility — test_cloud.py's polling pattern
    (`client.runs.create` + `client.runs.get`) exercises exactly this path.
    """
    if not body.assistant_id:
        raise HTTPException(status_code=422, detail="assistant_id is required")
    try:
        return create_background_run(thread_id, body.assistant_id, body.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/threads/{thread_id}/runs/{run_id}")
async def thread_run_get(thread_id: str, run_id: str):
    run = get_background_run(run_id)
    if not run or run.get("thread_id") != thread_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
