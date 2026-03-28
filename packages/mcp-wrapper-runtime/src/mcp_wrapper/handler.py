"""Base Lambda handler for MCP service wrappers.

Handles the full credential lifecycle:
1. Load service.env (non-secret config) into os.environ
2. Extract calling user identity from AgentCore event
3. Load service secrets (Category 2) and per-user OAuth (Category 3)
4. Refresh expired OAuth tokens via standard OAuth2
5. Build auth URL when user is not yet authenticated
6. Launch the MCP subprocess via mcp_lambda adapter
"""

from __future__ import annotations

import os
import sys
import time

from .config import ServiceConfig
from .credentials import CredentialManager
from .oauth import OAuthHelper


def _load_service_env() -> None:
    """Load ``service.env`` from the Lambda package into ``os.environ``.

    Uses ``service.local.env`` if present (gitignored, for local dev
    overrides), otherwise falls back to ``service.env``.

    The bundler copies these files alongside handler.py.  We load once
    at module import time so the values are available both to the handler
    (e.g. for OAuth URL building) and to the subprocess.
    """
    # In Lambda, the working directory is /var/task (where the bundle lives).
    # Try local override first, then committed file.
    candidates = [
        "service.local.env", "/var/task/service.local.env",
        "service.env", "/var/task/service.env",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key and key not in os.environ:
                        os.environ[key] = value
            break


# Load service.env once when the Lambda cold-starts.
_load_service_env()


class McpServiceHandler:
    """Reusable Lambda handler — one instance per service."""

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self._cred_manager: CredentialManager | None = None
        self._oauth_helper: OAuthHelper | None = None

    @property
    def cred_manager(self) -> CredentialManager:
        if self._cred_manager is None:
            self._cred_manager = CredentialManager()
        return self._cred_manager

    @property
    def oauth_helper(self) -> OAuthHelper:
        if self._oauth_helper is None:
            self._oauth_helper = OAuthHelper()
        return self._oauth_helper

    # ------------------------------------------------------------------ #
    # Entry point                                                         #
    # ------------------------------------------------------------------ #

    def handle(self, event, context):
        """AWS Lambda entrypoint."""
        # Health check
        if isinstance(event, dict) and (event.get("ping") or event.get("health")):
            return {
                "status": "ok",
                "handler": self.config.service_name,
                "python": sys.version.split()[0],
            }

        user_id = self._extract_user_id(event)
        env_vars = self._build_subprocess_env(user_id)

        # Lazy imports — keeps cold start fast when health-checking
        from mcp.client.stdio import StdioServerParameters
        from mcp_lambda import (
            BedrockAgentCoreGatewayTargetHandler,
            StdioServerAdapterRequestHandler,
        )

        # Resolve subprocess command.
        # Python module mode: python -m my_mcp.server
        # Custom command mode: /path/to/binary --stdio
        if self.config.mcp_module:
            cmd = sys.executable
            cmd_args = ["-m", self.config.mcp_module]
        elif self.config.command:
            cmd = self.config.command
            cmd_args = list(self.config.args)
        else:
            raise ValueError(
                "ServiceConfig must set either mcp_module (Python) "
                "or command (any language)"
            )

        server_params = StdioServerParameters(
            command=cmd,
            args=cmd_args,
            env=env_vars,
        )

        request_handler = StdioServerAdapterRequestHandler(server_params)
        event_handler = BedrockAgentCoreGatewayTargetHandler(request_handler)
        return event_handler.handle(event, context)

    # ------------------------------------------------------------------ #
    # User identity extraction                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_user_id(event) -> str | None:
        """Extract Cognito ``sub`` from the AgentCore event payload.

        Tries several common locations where AWS services place JWT claims.
        Returns None if identity cannot be determined (e.g. health checks).
        """
        if not isinstance(event, dict):
            return None

        # AgentCore Gateway / API Gateway v2 JWT authorizer
        rc = event.get("requestContext", {})
        authorizer = rc.get("authorizer", {})

        jwt_claims = authorizer.get("jwt", {}).get("claims", {})
        if jwt_claims.get("sub"):
            return str(jwt_claims["sub"])

        # API Gateway v1 Cognito authorizer
        if authorizer.get("claims", {}).get("sub"):
            return str(authorizer["claims"]["sub"])

        # Custom Lambda authorizer (flat claims)
        if authorizer.get("sub"):
            return str(authorizer["sub"])

        # Identity block (some AgentCore event formats)
        identity = event.get("identity", {})
        if isinstance(identity, dict) and identity.get("sub"):
            return str(identity["sub"])

        # Forwarded OIDC identity header
        headers = event.get("headers", {})
        if isinstance(headers, dict) and headers.get("x-amzn-oidc-identity"):
            return str(headers["x-amzn-oidc-identity"])

        return None

    # ------------------------------------------------------------------ #
    # Environment construction                                            #
    # ------------------------------------------------------------------ #

    def _build_subprocess_env(self, user_id: str | None) -> dict[str, str]:
        """Merge all three credential categories into a subprocess env dict."""
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        }

        # Category 1: passthrough (static config from Lambda env vars)
        for key in self.config.passthrough_env_vars:
            val = os.environ.get(key)
            if val:
                env[key] = val

        # Category 2: service-level secrets (Secrets Manager → env vars)
        secret_name = (
            self.config.service_secret_name
            or os.environ.get("SERVICE_SECRET_NAME", "")
        )
        if secret_name:
            try:
                service_secrets = self.cred_manager.load_service_secrets(secret_name)
                env.update(service_secrets)
            except Exception as exc:
                print(
                    f"[mcp-wrapper] Warning: service secrets load failed: {exc}",
                    file=sys.stderr,
                )

        # Category 3: per-user OAuth (must run AFTER category 2 — refresh
        # needs the client_secret which may come from service secrets)
        if self.config.oauth and self.config.access_token_env_var and user_id:
            self._inject_oauth_credentials(env, user_id)

        # Framework metadata
        env["SERVICE_NAME"] = self.config.service_name
        if user_id:
            env["OAUTH_USER_ID"] = user_id

        return {k: v for k, v in env.items() if v}

    # ------------------------------------------------------------------ #
    # OAuth credential injection                                          #
    # ------------------------------------------------------------------ #

    def _inject_oauth_credentials(self, env: dict, user_id: str) -> None:
        """Load, refresh, or initiate OAuth for the calling user."""
        creds = self.cred_manager.load_user_credentials(
            user_id, self.config.service_name
        )

        if creds and creds.get("access_token"):
            # Refresh if expiring within 60 seconds
            expires_at = creds.get("expires_at", 0)
            if expires_at and time.time() > expires_at - 60:
                refreshed = self._refresh_credentials(creds, env, user_id)
                if refreshed:
                    creds = refreshed

            if creds.get("access_token"):
                env[self.config.access_token_env_var] = creds["access_token"]
                env["OAUTH_AUTHENTICATED"] = "true"
                return

        # Not authenticated — provide auth URL for the MCP server to surface
        env["OAUTH_AUTHENTICATED"] = "false"
        try:
            auth_url = self.oauth_helper.build_auth_url(user_id, self.config, env)
            env["OAUTH_AUTH_URL"] = auth_url
        except Exception as exc:
            print(
                f"[mcp-wrapper] Warning: failed to build auth URL: {exc}",
                file=sys.stderr,
            )

    def _refresh_credentials(
        self, creds: dict, env: dict, user_id: str
    ) -> dict | None:
        """Attempt a standard OAuth2 refresh token grant."""
        oauth = self.config.oauth
        refresh_token = creds.get("refresh_token")
        if not refresh_token or not oauth:
            return None

        try:
            # client_id and client_secret may come from Lambda env vars
            # (Category 1) or service secrets (Category 2, already in env dict)
            client_id = (
                env.get(oauth.client_id_env, "")
                or os.environ.get(oauth.client_id_env, "")
            )
            client_secret = env.get(oauth.client_secret_key, "")

            token_endpoint = OAuthHelper.resolve_endpoint(
                oauth.token_endpoint, self.config
            )

            refreshed = OAuthHelper.refresh_token(
                token_endpoint=token_endpoint,
                refresh_token=refresh_token,
                client_id=client_id,
                client_secret=client_secret or None,
                scopes=oauth.scopes,
            )

            self.cred_manager.store_user_credentials(
                user_id, self.config.service_name, refreshed
            )
            return refreshed
        except Exception as exc:
            print(
                f"[mcp-wrapper] Warning: token refresh failed: {exc}",
                file=sys.stderr,
            )
            return None
