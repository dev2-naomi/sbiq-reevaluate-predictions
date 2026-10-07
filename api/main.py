"""FastAPI application — LangGraph Platform-compatible API for AWS Lambda.

Served via the AWS Lambda Web Adapter (see api/Dockerfile): this process
runs as a normal uvicorn HTTP server, and the adapter (a Lambda extension)
translates every Lambda invocation into an HTTP request against it — there
is no separate Python "Lambda handler" function needed (a plain Mangum
handler would in fact never be invoked under this architecture; see
api/services/runs.py's module docstring for how that bit us with an async
self-invoke background-run design before this file was simplified).
"""

from __future__ import annotations

import os

from api.secrets import load_secrets

load_secrets()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.platform.assistants import router as assistants_router
from api.platform.runs import router as runs_router
from api.platform.threads import router as threads_router

app = FastAPI(
    title="Reevaluate Preconditions Agent API",
    description="LangGraph Platform-compatible serverless agent API for reevaluate-preconditions",
    version="0.1.0",
)

# Lambda Function URL adds its own CORS headers. Enabling FastAPI CORS as
# well produces duplicate Access-Control-Allow-Origin values, which browsers
# reject.
if not os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    _cors_origins = os.environ.get("CORS_ALLOW_ORIGINS", "*")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(assistants_router)
app.include_router(threads_router)
app.include_router(runs_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    expected_key = os.environ.get("API_KEY", "").strip()
    if request.method == "OPTIONS":
        return await call_next(request)
    if expected_key and request.url.path != "/health":
        provided = request.headers.get("x-api-key", "")
        if provided != expected_key:
            return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
    return await call_next(request)
