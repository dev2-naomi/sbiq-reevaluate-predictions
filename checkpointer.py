"""Shared LangGraph checkpointer for local dev and AWS Lambda.

Selection is entirely env-driven so the same ``agent.py:_compile_agent``
factory works unchanged across ``langgraph dev``, LangGraph Cloud, the local
FastAPI shim (``api/``), and the AWS Lambda deployment.

Pattern ported from the LG-docsOrch / monte-carlo-intelligence AWS migration
playbook (docs/LANGSMITH_TO_AWS_MIGRATION_PLAYBOOK.md §4.4).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver


@lru_cache(maxsize=1)
def get_checkpointer() -> "BaseCheckpointSaver | None":
    """Return a module-scoped checkpointer when persistence is configured.

    - ``CHECKPOINT_TABLE_NAME`` set -> DynamoDB (Lambda / AWS)
    - ``CHECKPOINT_IN_MEMORY=true`` -> InMemorySaver (local API dev/tests)
    - otherwise -> None (stateless / langgraph dev, which manages its own
      persistence and never calls this module)

    Reevaluate-preconditions' state (raw predicted_conditions_json +
    updated_manifest_json strings, plus the growing document_requests
    list) regularly exceeds DynamoDB's 400KB item cap for a single
    checkpoint on a real loan (confirmed testing a 24-document-request
    loan — the checkpoint write raised a ValidationException that aborted
    an otherwise-successful run). ``langgraph_checkpoint_aws``'s
    DynamoDBSaver has a built-in S3-offload path for payloads over 350KB,
    but it's a no-op unless a bucket is supplied — ``CHECKPOINT_S3_BUCKET``
    (see infra/stacks/reevaluate_stack.py's CheckpointOffloadBucket) wires
    that up. Falls back to plain DynamoDB-only storage if unset (e.g.
    local dev without the bucket configured), which is fine for small
    test fixtures but will hit the same 400KB failure on large real
    inputs.
    """
    table_name = os.environ.get("CHECKPOINT_TABLE_NAME", "").strip()
    if table_name:
        from langgraph_checkpoint_aws import DynamoDBSaver

        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))
        s3_bucket = os.environ.get("CHECKPOINT_S3_BUCKET", "").strip()
        s3_offload_config = {"bucket_name": s3_bucket} if s3_bucket else None
        return DynamoDBSaver(
            table_name=table_name,
            region_name=region,
            s3_offload_config=s3_offload_config,
        )

    if os.environ.get("CHECKPOINT_IN_MEMORY", "").strip().lower() in ("1", "true", "yes"):
        from langgraph.checkpoint.memory import InMemorySaver

        return InMemorySaver()

    return None
