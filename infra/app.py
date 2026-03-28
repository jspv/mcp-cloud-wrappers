#!/usr/bin/env python3
"""CDK app entry point — auto-discovers services and creates stacks.

Adding a new service = creating a directory under infra/lambda/services/.
No edits to this file are needed.

Usage:
  make deploy-shared                    # shared infra (first time)
  make deploy-service SERVICE=msgraph   # a specific service
  make deploy-all                       # everything
"""

from __future__ import annotations

import json
import os
import sys

# Add infra/ to path so cdk_constructs/stacks resolve as packages.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aws_cdk as cdk

from stacks.shared import SharedInfraStack
from stacks.service import ServiceStack

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAMBDA_SERVICES = os.path.join(_HERE, "lambda", "services")

# Framework-recognized keys in service.env (consumed by CDK, not passed to Lambda).
_FRAMEWORK_KEYS = {"LAMBDA_TIMEOUT", "LAMBDA_MEMORY"}


def _parse_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=VALUE file, skipping comments and blank lines."""
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                values[key] = value
    return values


def _discover_services() -> list[dict]:
    """Scan infra/lambda/services/ for service directories.

    A valid service directory must contain handler.py.
    """
    services = []
    if not os.path.isdir(_LAMBDA_SERVICES):
        return services
    for name in sorted(os.listdir(_LAMBDA_SERVICES)):
        service_dir = os.path.join(_LAMBDA_SERVICES, name)
        if not os.path.isdir(service_dir):
            continue
        if not os.path.isfile(os.path.join(service_dir, "handler.py")):
            continue

        # Parse service.env (or service.local.env) for settings.
        env_file = os.path.join(service_dir, "service.local.env")
        if not os.path.isfile(env_file):
            env_file = os.path.join(service_dir, "service.env")
        env_values = _parse_env_file(env_file)

        # Load tool definitions from tools.json if present.
        tools_file = os.path.join(service_dir, "tools.json")
        tools = None
        if os.path.isfile(tools_file):
            with open(tools_file) as f:
                tools = json.load(f)

        # Load OAuth provider config from oauth.json if present.
        oauth_file = os.path.join(service_dir, "oauth.json")
        oauth_config = None
        if os.path.isfile(oauth_file):
            with open(oauth_file) as f:
                oauth_config = json.load(f)
            # Merge service.env values needed for endpoint_params resolution
            if oauth_config.get("endpoint_params"):
                resolved_env = {}
                for placeholder, env_key in oauth_config["endpoint_params"].items():
                    resolved_env[env_key] = env_values.get(env_key, "")
                oauth_config["resolved_env"] = resolved_env

        services.append({
            "name": name,
            "dir": service_dir,
            "timeout": int(env_values.pop("LAMBDA_TIMEOUT", "120")),
            "memory": int(env_values.pop("LAMBDA_MEMORY", "512")),
            "tools": tools,
            "oauth": oauth_config,
        })
    return services


# ------------------------------------------------------------------ #
# App                                                                  #
# ------------------------------------------------------------------ #

app = cdk.App()

prefix = app.node.try_get_context("prefix") or "mcp-wrappers"

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

# Discover services first (need OAuth configs for shared stack).
all_services = _discover_services()

# Aggregate OAuth configs for the auth setup page.
service_oauth_configs = []
for svc in all_services:
    if svc.get("oauth"):
        service_oauth_configs.append({
            "service_name": svc["name"],
            "service_secret_name": f"{prefix}-{svc['name']}-service-secrets",
            **svc["oauth"],
        })

# Shared infrastructure (deploy once).
shared = SharedInfraStack(
    app, f"{prefix}-shared", env=env,
    service_oauth_configs=service_oauth_configs or None,
)

# Auto-discover and create a stack for each service.
for svc in all_services:
    ServiceStack(
        app,
        f"{prefix}-{svc['name']}",
        env=env,
        service_name=svc["name"],
        handler_source_dir=svc["dir"],
        discovery_url=shared.discovery_url,
        oauth_callback_url=shared.oauth_callback_url,
        oauth_state_table_arn=shared.oauth_state_table.table_arn,
        oauth_state_table_name=shared.oauth_state_table.table_name,
        lambda_timeout=svc["timeout"],
        lambda_memory=svc["memory"],
        tool_definitions=svc["tools"],
        auth_setup_url=getattr(shared, "auth_setup_url", ""),
        lambda_environment={
            "SERVICE_SECRET_NAME": f"{prefix}-{svc['name']}-service-secrets",
        },
    )

app.synth()
