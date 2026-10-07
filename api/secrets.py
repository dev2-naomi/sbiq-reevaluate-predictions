"""Load secrets from AWS Secrets Manager on Lambda cold start.

Ported from LG-docsOrch's AWS migration pattern
(docs/LANGSMITH_TO_AWS_MIGRATION_PLAYBOOK.md §4.3).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)
_loaded = False

# Keys hydrated from Secrets Manager (AGENT_SECRETS_ARN) into os.environ at
# cold start, so the rest of the agent code (agent.py, tools/, step_loader.py)
# stays unchanged whether it's running under `langgraph dev`, LangGraph
# Cloud, or this Lambda. Compiled from the vars actually read by the
# runtime code paths (agent.py's LLM_PROVIDER + ANTHROPIC_API_KEY/
# ANTHROPIC_MODEL or OPENAI_API_KEY/OPENAI_MODEL, plus LangSmith tracing
# vars, which LangChain reads implicitly with no explicit os.getenv call)
# — not the env.example-only dev-mode override vars. Keep in sync with
# infra/stacks/reevaluate_stack.py:secret_keys.
SECRET_KEYS = (
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "LANGCHAIN_API_KEY",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_PROJECT",
    "API_KEY",
)


def load_secrets() -> None:
    global _loaded
    if _loaded:
        return

    secret_arn = os.environ.get("AGENT_SECRETS_ARN", "").strip()
    if not secret_arn:
        _loaded = True
        return

    try:
        import boto3

        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_arn)
        payload = json.loads(resp["SecretString"])
        for key in SECRET_KEYS:
            value = payload.get(key)
            if value and not os.environ.get(key):
                os.environ[key] = str(value)
        logger.info("Loaded agent secrets from Secrets Manager")
    except Exception:
        logger.exception("Failed to load secrets from %s", secret_arn)
    finally:
        _loaded = True
