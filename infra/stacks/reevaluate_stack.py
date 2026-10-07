"""AWS CDK stack for the reevaluate-preconditions serverless agent.

Adapted from the LG-docsOrch / monte-carlo-intelligence AWS migration
playbook (docs/LANGSMITH_TO_AWS_MIGRATION_PLAYBOOK.md). Deliberately
simpler than LG-docsOrch's stack: no VPC/ECS Fargate worker, since this
agent's runs finish in ~50-80s (README) — well under Lambda's 900s
ceiling — so per the playbook's own §4.6 guidance ("if your agent always
finishes well under 15 minutes, skip the Fargate worker"), a plain Lambda
+ DynamoDB + Secrets Manager stack is sufficient.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_ecr_assets as ecr_assets,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

REPO_ROOT = Path(__file__).resolve().parents[2]

_DOCKER_BUILD_EXCLUDES = [
    ".venv",
    "venv",
    ".git",
    "test_results",
    "nyarko_reeval",
    "agent-transcripts",
    "infra/.venv",
    "infra/cdk.out",
    # langgraph dev's local checkpoint store — irrelevant to the Lambda
    # image, which uses DynamoDB via checkpointer.py.
    ".langgraph_api",
]
_DOCKER_PLATFORM = ecr_assets.Platform.LINUX_ARM64


def _deploy_env(name: str, default: str = "") -> str:
    """Read deploy-time env (source .env before cdk deploy)."""
    return os.environ.get(name, default)


class ReevaluatePreconditionsStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, stage: str = "prod", **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        is_prod = stage == "prod"
        suffix = "" if is_prod else f"-{stage}"

        checkpoint_table = dynamodb.Table(
            self,
            "CheckpointTable",
            table_name=f"reevaluate-preconditions-checkpoints{suffix}",
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # langgraph_checkpoint_aws's DynamoDBSaver offloads any single
        # checkpoint payload over 350KB to S3 instead of DynamoDB (which
        # hard-caps items at 400KB) — see checkpointer.py. A real loan's
        # state (predicted_conditions_json + updated_manifest_json +
        # document_requests) regularly exceeds that on its own; without
        # this bucket configured, the saver still tries DynamoDB anyway
        # and the *entire run* fails with a DynamoDB ValidationException
        # right as it's about to persist its final, otherwise-successful
        # result (reproduced testing against a real 24-document-request
        # loan). Lifecycle-expired quickly since checkpoints are only
        # useful for the lifetime of a single run (no HITL/resume here).
        checkpoint_offload_bucket = s3.Bucket(
            self,
            "CheckpointOffloadBucket",
            bucket_name=f"reevaluate-preconditions-checkpoint-offload{suffix}-{self.account}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        # Real values are intentionally NOT passed to CDK/CloudFormation
        # here. GenerateSecretString seeds an empty placeholder once, at
        # creation; real values are pushed via
        # `aws secretsmanager put-secret-value` in scripts/deploy.sh, after
        # `cdk deploy` — a plain API call, never a CloudFormation resource
        # property, so it can't end up in a synthesized template.
        #
        # Keep in sync with api/secrets.py:SECRET_KEYS.
        secret_keys = [
            "LLM_PROVIDER",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_MODEL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "LANGCHAIN_API_KEY",
            "LANGCHAIN_TRACING_V2",
            "LANGCHAIN_PROJECT",
            "API_KEY",
        ]
        agent_secrets = secretsmanager.Secret(
            self,
            "AgentSecrets",
            secret_name=(None if is_prod else f"reevaluate-preconditions-secrets{suffix}"),
            description=(
                f"reevaluate-preconditions agent API keys and tokens ({stage}). Values are "
                "managed out-of-band via `aws secretsmanager put-secret-value` (see "
                "scripts/deploy.sh), not by CloudFormation."
            ),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps({key: "" for key in secret_keys}),
                generate_string_key="_cfn_placeholder",
                exclude_punctuation=True,
            ),
        )

        agent_fn = lambda_.DockerImageFunction(
            self,
            "AgentFunction",
            function_name=(None if is_prod else f"reevaluate-preconditions-agent{suffix}"),
            description=f"reevaluate-preconditions LangGraph Platform-compatible agent API ({stage})",
            code=lambda_.DockerImageCode.from_image_asset(
                str(REPO_ROOT),
                file="api/Dockerfile",
                exclude=_DOCKER_BUILD_EXCLUDES,
                platform=_DOCKER_PLATFORM,
            ),
            memory_size=2048,
            # 900s is the Lambda platform maximum. Real runs take ~50-80s
            # (README) — no Fargate worker needed; see module docstring.
            timeout=Duration.seconds(900),
            architecture=lambda_.Architecture.ARM_64,
            environment={
                # Non-secret config only. Secrets are intentionally NOT set
                # here — api/secrets.py fetches them from AGENT_SECRETS_ARN
                # at cold start instead.
                "CHECKPOINT_TABLE_NAME": checkpoint_table.table_name,
                "CHECKPOINT_S3_BUCKET": checkpoint_offload_bucket.bucket_name,
                "AGENT_SECRETS_ARN": agent_secrets.secret_arn,
                "LANGSMITH_PROJECT": _deploy_env("LANGCHAIN_PROJECT", f"reevaluate-preconditions{suffix}"),
                "LANGSMITH_TRACING": _deploy_env("LANGCHAIN_TRACING_V2", "true"),
                "CORS_ALLOW_ORIGINS": _deploy_env("CORS_ALLOW_ORIGINS", "*"),
                "AWS_LWA_INVOKE_MODE": "response_stream",
                "AWS_LWA_READINESS_CHECK_PATH": "/health",
                "PORT": "8080",
            },
        )

        checkpoint_table.grant_read_write_data(agent_fn)
        checkpoint_offload_bucket.grant_read_write(agent_fn)
        agent_secrets.grant_read(agent_fn)

        # Lambda Function URL — not API Gateway (which caps integration
        # timeouts at 30s, breaking streaming).
        function_url = agent_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.ALL],
                allowed_headers=["content-type", "authorization", "x-api-key"],
                allow_credentials=True,
            ),
        )

        CfnOutput(self, "ApiUrl", value=function_url.url)
        CfnOutput(self, "FunctionUrl", value=function_url.url)
        CfnOutput(self, "CheckpointTableName", value=checkpoint_table.table_name)
        CfnOutput(self, "CheckpointOffloadBucketName", value=checkpoint_offload_bucket.bucket_name)
        CfnOutput(self, "AgentSecretsArn", value=agent_secrets.secret_arn)
        CfnOutput(self, "LambdaFunctionName", value=agent_fn.function_name)
        CfnOutput(self, "Stage", value=stage)
