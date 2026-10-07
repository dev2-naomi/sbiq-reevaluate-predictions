"""Thread metadata store (minimal LangGraph Platform compatibility).

Ported from the LG-docsOrch / monte-carlo-intelligence AWS migration
reference — generic, not agent-specific. Uses the same DynamoDB table as
the checkpointer (CHECKPOINT_TABLE_NAME) with a PK/SK layout that coexists
with checkpoint items (thread metadata uses SK="METADATA";
langgraph_checkpoint_aws owns its own SK scheme for actual checkpoints).

Background-run records live in the same table under PK=f"RUN#{run_id}", so
a client polling `GET /threads/{thread_id}/runs/{run_id}` gets an O(1)
lookup.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

# Terminal run statuses — a polling client should stop polling once one of
# these is reached. reevaluate-preconditions has no interrupt()/HITL path,
# so "interrupted" never actually occurs here, but it's kept in the
# vocabulary for parity with sibling agents' clients.
RUN_TERMINAL_STATUSES = frozenset({"success", "error", "timeout", "interrupted"})

# Margin under DynamoDB's hard 400KB item cap for a run record's
# `input_payload` field. Nothing reads this back to re-dispatch a run —
# runs execute synchronously inline (see api/services/runs.py) — so it's
# stored purely for debugging visibility via GET /threads/{id}/runs/{id}.
# A real loan's predicted_conditions_json + updated_manifest_json can
# comfortably exceed this on its own: hit in practice testing a
# 24-document-request loan (~190KB combined input), which raised an
# uncaught DynamoDB ValidationException ("Item size has exceeded the
# maximum allowed size") straight out of put_item in create_run(), before
# the graph even started running. Capping it here (rather than in every
# caller) keeps both store implementations safe by construction.
_MAX_INPUT_PAYLOAD_BYTES = 300_000


def _capped_input_payload(run_body: dict[str, Any]) -> dict[str, Any]:
    try:
        size = len(json.dumps(run_body, default=str))
    except Exception:  # noqa: BLE001 - if we can't even measure it, drop it
        size = _MAX_INPUT_PAYLOAD_BYTES + 1
    if size <= _MAX_INPUT_PAYLOAD_BYTES:
        return run_body
    return {
        "truncated": True,
        "assistant_id": run_body.get("assistant_id"),
        "reason": (
            f"Original input ({size} bytes) exceeds this run record's "
            f"storage limit ({_MAX_INPUT_PAYLOAD_BYTES} bytes) and was "
            "omitted. The run still executed against the real input — "
            "this only affects what GET /threads/{thread_id}/runs/{run_id} "
            "echoes back; fetch GET /threads/{thread_id}/state for the "
            "full final state instead."
        ),
    }


class ThreadStore:
    def create(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def get(self, thread_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def record_run(self, thread_id: str, *, assistant_id: str, metadata: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        run_body: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        raise NotImplementedError


class InMemoryThreadStore(ThreadStore):
    def __init__(self) -> None:
        self._threads: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}

    def create(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        thread_id = str(uuid.uuid4())
        record = {
            "thread_id": thread_id,
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": metadata or {},
            "status": "idle",
        }
        self._threads[thread_id] = record
        return record

    def get(self, thread_id: str) -> dict[str, Any] | None:
        return self._threads.get(thread_id)

    def record_run(self, thread_id: str, *, assistant_id: str, metadata: dict[str, Any] | None = None) -> None:
        record = self._threads.get(thread_id)
        if not record:
            return
        merged = dict(record.get("metadata") or {})
        merged["assistant_id"] = assistant_id
        if metadata:
            merged.update(metadata)
        record["metadata"] = merged
        record["status"] = "busy"

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        run_body: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        record = {
            "run_id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "status": "pending",
            "input_payload": _capped_input_payload(run_body),
            "created_at": now,
            "updated_at": now,
            "error": None,
            "result": None,
        }
        self._runs[run_id] = record
        return dict(record)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        record = self._runs.get(run_id)
        return dict(record) if record else None

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        record = self._runs.get(run_id)
        if not record:
            return
        if status is not None:
            record["status"] = status
        if result is not None:
            record["result"] = result
        if error is not None:
            record["error"] = error
        record["updated_at"] = datetime.now(UTC).isoformat()


class DynamoDBThreadStore(ThreadStore):
    def __init__(self, table_name: str, *, region_name: str | None = None) -> None:
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def create(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        thread_id = str(uuid.uuid4())
        record = {
            "PK": f"THREAD#{thread_id}",
            "SK": "METADATA",
            "thread_id": thread_id,
            "created_at": datetime.now(UTC).isoformat(),
            "metadata": metadata or {},
            "status": "idle",
        }
        self._table.put_item(Item=record)
        return {
            "thread_id": thread_id,
            "created_at": record["created_at"],
            "metadata": record["metadata"],
            "status": record["status"],
        }

    def get(self, thread_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"PK": f"THREAD#{thread_id}", "SK": "METADATA"})
        item = resp.get("Item")
        if not item:
            return None
        return {
            "thread_id": item["thread_id"],
            "created_at": item.get("created_at", ""),
            "metadata": item.get("metadata", {}),
            "status": item.get("status", "idle"),
        }

    def record_run(self, thread_id: str, *, assistant_id: str, metadata: dict[str, Any] | None = None) -> None:
        record = self.get(thread_id)
        if not record:
            return
        merged = dict(record.get("metadata") or {})
        merged["assistant_id"] = assistant_id
        if metadata:
            merged.update(metadata)
        self._table.update_item(
            Key={"PK": f"THREAD#{thread_id}", "SK": "METADATA"},
            UpdateExpression="SET metadata = :metadata, #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":metadata": merged,
                ":status": "busy",
            },
        )

    def create_run(
        self,
        thread_id: str,
        *,
        assistant_id: str,
        run_body: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        item = {
            "PK": f"RUN#{run_id}",
            "SK": "METADATA",
            "run_id": run_id,
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "status": "pending",
            "input_payload": _capped_input_payload(run_body),
            "created_at": now,
            "updated_at": now,
            "error": None,
            "result": None,
        }
        self._table.put_item(Item=item)
        return {k: v for k, v in item.items() if k not in ("PK", "SK")}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        resp = self._table.get_item(Key={"PK": f"RUN#{run_id}", "SK": "METADATA"})
        item = resp.get("Item")
        if not item:
            return None
        return {k: v for k, v in item.items() if k not in ("PK", "SK")}

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {"updated_at": datetime.now(UTC).isoformat()}
        if status is not None:
            updates["status"] = status
        if result is not None:
            updates["result"] = result
        if error is not None:
            updates["error"] = error

        expr_names = {f"#{k}": k for k in updates}
        expr_values = {f":{k}": v for k, v in updates.items()}
        set_clause = ", ".join(f"#{k} = :{k}" for k in updates)
        self._table.update_item(
            Key={"PK": f"RUN#{run_id}", "SK": "METADATA"},
            UpdateExpression=f"SET {set_clause}",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )


_thread_store: ThreadStore | None = None


def get_thread_store() -> ThreadStore:
    global _thread_store
    if _thread_store is not None:
        return _thread_store

    table_name = os.environ.get("CHECKPOINT_TABLE_NAME", "").strip()
    if table_name:
        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
        _thread_store = DynamoDBThreadStore(table_name, region_name=region)
    else:
        _thread_store = InMemoryThreadStore()
    return _thread_store
