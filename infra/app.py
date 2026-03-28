#!/usr/bin/env python3
"""CDK app entry point — shared infra + per-service stacks.

Usage:
  uv run cdk synth
  uv run cdk deploy mcp-wrappers-shared
  uv run cdk deploy mcp-wrappers-msgraph -c microsoft_client_id=YOUR_CLIENT_ID
"""

from __future__ import annotations

import os
import sys

# Add infra/ to path so constructs/stacks resolve as packages
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aws_cdk as cdk

from stacks.shared import SharedInfraStack
from stacks.service import ServiceStack

app = cdk.App()

prefix = app.node.try_get_context("prefix") or "mcp-wrappers"

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

# ------------------------------------------------------------------ #
# Shared infrastructure (deploy once)                                  #
# ------------------------------------------------------------------ #
shared = SharedInfraStack(app, f"{prefix}-shared", env=env)

# ------------------------------------------------------------------ #
# msgraph service                                                      #
# ------------------------------------------------------------------ #
_HERE = os.path.dirname(os.path.abspath(__file__))
_LAMBDA_SERVICES = os.path.join(_HERE, "lambda", "services")

ServiceStack(
    app,
    f"{prefix}-msgraph",
    env=env,
    service_name="msgraph",
    handler_source_dir=os.path.join(_LAMBDA_SERVICES, "msgraph"),
    discovery_url=shared.discovery_url,
    oauth_callback_url=shared.oauth_callback_url,
    oauth_state_table_arn=shared.oauth_state_table.table_arn,
    oauth_state_table_name=shared.oauth_state_table.table_name,
    lambda_timeout=120,
    lambda_memory=512,
    lambda_environment={
        "MICROSOFT_CLIENT_ID": app.node.try_get_context("microsoft_client_id") or "",
        "MICROSOFT_TENANT_ID": app.node.try_get_context("microsoft_tenant_id") or "organizations",
        "SERVICE_SECRET_NAME": f"{prefix}-msgraph-service-secrets",
    },
)

# ------------------------------------------------------------------ #
# Add future services here:                                            #
#                                                                      #
# ServiceStack(                                                        #
#     app,                                                             #
#     f"{prefix}-google-calendar",                                     #
#     env=env,                                                         #
#     service_name="google-calendar",                                  #
#     handler_source_dir=os.path.join(_LAMBDA_SERVICES, "gcal"),       #
#     discovery_url=shared.discovery_url,                              #
#     oauth_callback_url=shared.oauth_callback_url,                    #
#     oauth_state_table_arn=shared.oauth_state_table.table_arn,        #
#     oauth_state_table_name=shared.oauth_state_table.table_name,      #
#     lambda_environment={                                             #
#         "GOOGLE_CLIENT_ID": app.node.try_get_context("...") or "",   #
#         "SERVICE_SECRET_NAME": f"{prefix}-gcal-service-secrets",     #
#     },                                                               #
# )                                                                    #
# ------------------------------------------------------------------ #

app.synth()
