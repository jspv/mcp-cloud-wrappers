#!/usr/bin/env python3
"""CDK app entry point — shared infra + per-service stacks.

Usage:
  make deploy-shared          # shared infra (Cognito, DCR, OAuth callback)
  make deploy-service SERVICE=msgraph   # a specific service
  make deploy-all             # everything
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
        # Per-service config comes from service.env in the service directory.
        # Per-service secrets come from Secrets Manager.
        # Only framework plumbing goes here.
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
