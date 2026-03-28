"""Service and OAuth provider configuration dataclasses.

These are the only things a service author needs to define. Everything else
is handled by the framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Describes an external OAuth2 provider for backend authentication.

    Endpoint URLs may contain ``{placeholder}`` tokens that are resolved
    at runtime from environment variables via ``endpoint_params``.
    """

    provider_name: str
    auth_endpoint: str
    token_endpoint: str
    scopes: list[str]

    # Env var name whose value holds the OAuth client_id
    client_id_env: str = ""
    # Key name inside the service secret JSON that holds client_secret
    client_secret_key: str = ""

    # Maps placeholder names in endpoint URLs to env var names.
    # e.g. {"tenant_id": "MICROSOFT_TENANT_ID"}
    endpoint_params: dict[str, str] = field(default_factory=dict)

    uses_pkce: bool = True
    # Extra query params appended to the authorization URL.
    extra_auth_params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServiceConfig:
    """Everything the framework needs to wrap an MCP service.

    Adding a new service = defining one of these + a thin Lambda handler.
    """

    # Identity
    service_name: str

    # How to launch the MCP server subprocess.
    #
    # For Python packages (most common):
    #   mcp_module="my_mcp.server"  →  python -m my_mcp.server
    #
    # For non-Python servers, set mcp_module=None and use command/args:
    #   command="/var/task/bin/my-mcp-server", args=["--stdio"]
    #
    mcp_module: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)

    # Category 1: static config — env var names passed through from Lambda env
    passthrough_env_vars: list[str] = field(default_factory=list)

    # Category 2: service-level secrets — Secrets Manager secret name.
    # JSON keys in the secret become env vars in the subprocess.
    # Resolved from SERVICE_SECRET_NAME env var at runtime if not set here.
    service_secret_name: str | None = None

    # Category 3: per-user OAuth credentials
    oauth: OAuthProviderConfig | None = None
    # Env var name injected into subprocess with the user's access token
    access_token_env_var: str | None = None

    # Lambda tuning
    lambda_timeout: int = 120
    lambda_memory: int = 512
