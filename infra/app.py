#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.reevaluate_stack import ReevaluatePreconditionsStack

# Pin account/region explicitly instead of the CFN pseudo-params or
# CDK_DEFAULT_ACCOUNT/REGION — the `cdk` CLI always recomputes CDK_DEFAULT_*
# from the ambient AWS profile right before invoking this file, silently
# overwriting any override under those same names.
#
# Same AWS account as LG-docsOrch / monte-carlo-intelligence / sbiq-conditions-ai
# (828351637694 / us-east-2) — that account hosts other unrelated projects,
# so this stack's resource names are reevaluate-preconditions-specific to
# avoid colliding with siblings' stacks.
KNOWN_ACCOUNT = "828351637694"
KNOWN_REGION = "us-east-2"

ACCOUNT = os.environ.get("REEVALUATE_AWS_ACCOUNT_ID", KNOWN_ACCOUNT)
REGION = os.environ.get("REEVALUATE_AWS_REGION", KNOWN_REGION)

# Stage separation: REEVALUATE_STAGE=dev|prod selects which stack instance
# to synthesize/deploy. Defaults to "prod".
STAGE = os.environ.get("REEVALUATE_STAGE", "prod").strip().lower()
if STAGE not in ("dev", "prod"):
    raise SystemExit(f"REEVALUATE_STAGE must be 'dev' or 'prod', got: {STAGE!r}")

print(f"[infra/app.py] Targeting account={ACCOUNT} region={REGION} stage={STAGE}")
if ACCOUNT != KNOWN_ACCOUNT or REGION != KNOWN_REGION:
    print(
        f"[infra/app.py] NOTE: overridden via REEVALUATE_AWS_ACCOUNT_ID/REEVALUATE_AWS_REGION "
        f"(default is account={KNOWN_ACCOUNT} region={KNOWN_REGION})"
    )

STACK_ID = "ReevaluatePreconditionsStack" if STAGE == "prod" else "ReevaluatePreconditionsStack-Dev"

app = cdk.App()
ReevaluatePreconditionsStack(
    app,
    STACK_ID,
    stage=STAGE,
    env=cdk.Environment(account=ACCOUNT, region=REGION),
)
app.synth()
