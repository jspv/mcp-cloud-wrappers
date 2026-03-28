"""Lambda handler for the msgraph MCP service.

OAuth provider config is loaded from oauth.json (single source of truth).
Everything else is handled by the framework's McpServiceHandler.
"""

from __future__ import annotations

from mcp_wrapper import McpServiceHandler, ServiceConfig, load_oauth_json

config = ServiceConfig(
    service_name="msgraph",
    mcp_module="msgraph_mcp.server",
    passthrough_env_vars=[
        "MICROSOFT_TENANT_ID",      # Category 1: non-secret config (from service.env)
    ],
    service_secret_name="{prefix}-msgraph-service-secrets",
    oauth=load_oauth_json(),        # reads oauth.json — single source of truth
    access_token_env_var="GRAPH_ACCESS_TOKEN",
)

_handler = McpServiceHandler(config)


def handler(event, context):
    return _handler.handle(event, context)
