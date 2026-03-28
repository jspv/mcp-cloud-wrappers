"""Lambda handler for the msgraph MCP service.

This is the entirety of what a service author writes for the Lambda side.
Everything else is handled by the framework's McpServiceHandler.
"""

from __future__ import annotations

from mcp_wrapper import McpServiceHandler, OAuthProviderConfig, ServiceConfig

config = ServiceConfig(
    service_name="msgraph",
    mcp_module="msgraph_mcp.server",
    passthrough_env_vars=[
        "MICROSOFT_TENANT_ID",      # Category 1: non-secret config (Lambda env var)
        # MICROSOFT_CLIENT_ID comes from the service secret (Category 2)
    ],
    service_secret_name="{prefix}-msgraph-service-secrets",
    oauth=OAuthProviderConfig(
        provider_name="microsoft",
        auth_endpoint="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
        token_endpoint="https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        scopes=["User.Read", "Mail.ReadWrite", "Calendars.Read", "offline_access"],
        client_id_env="MICROSOFT_CLIENT_ID",
        client_secret_key="MICROSOFT_CLIENT_SECRET",
        endpoint_params={"tenant_id": "MICROSOFT_TENANT_ID"},
        uses_pkce=True,
    ),
    access_token_env_var="GRAPH_ACCESS_TOKEN",
)

_handler = McpServiceHandler(config)


def handler(event, context):
    return _handler.handle(event, context)
