"""Standard OAuth2 helpers — no service-specific SDKs.

Handles:
- PKCE code challenge generation
- Authorization URL construction
- Authorization code exchange
- Refresh token grant
- DynamoDB state management for pending flows
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
from urllib.parse import urlencode

from .config import ServiceConfig


class OAuthHelper:
    """Framework-level OAuth2 operations backed by DynamoDB for state."""

    def __init__(self) -> None:
        self._dynamodb = None
        self._table_name = os.environ.get("OAUTH_STATE_TABLE", "")
        self._callback_url = os.environ.get("OAUTH_CALLBACK_URL", "")

    @property
    def _table(self):
        if self._dynamodb is None:
            import boto3

            self._dynamodb = boto3.resource("dynamodb")
        return self._dynamodb.Table(self._table_name)

    # ------------------------------------------------------------------ #
    # PKCE                                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def generate_pkce() -> tuple[str, str]:
        """Generate PKCE code_verifier and code_challenge (S256)."""
        code_verifier = secrets.token_urlsafe(64)[:128]
        digest = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return code_verifier, code_challenge

    # ------------------------------------------------------------------ #
    # Authorization URL                                                   #
    # ------------------------------------------------------------------ #

    def build_auth_url(
        self,
        user_id: str,
        config: ServiceConfig,
        env: dict[str, str] | None = None,
    ) -> str:
        """Build an OAuth authorization URL and persist state in DynamoDB.

        *env* is the merged subprocess environment dict.  ``client_id`` is
        resolved from *env* first (covers service secrets loaded as Category 2),
        then falls back to ``os.environ`` (Lambda env vars / Category 1).
        """
        oauth = config.oauth
        state = secrets.token_urlsafe(32)
        merged = env or {}

        code_verifier, code_challenge = (
            self.generate_pkce() if oauth.uses_pkce else (None, None)
        )

        client_id = (
            merged.get(oauth.client_id_env, "")
            or os.environ.get(oauth.client_id_env, "")
        )

        # Persist state for the callback to validate
        item: dict = {
            "state": state,
            "user_id": user_id,
            "service_name": config.service_name,
            "token_endpoint": self.resolve_endpoint(oauth.token_endpoint, config),
            "client_id": client_id,
            "client_secret_key": oauth.client_secret_key,
            "service_secret_name": (
                config.service_secret_name.replace(
                    "{prefix}",
                    os.environ.get("SECRET_PREFIX", "mcp-wrappers"),
                )
                if config.service_secret_name
                else ""
            ),
            "scopes": " ".join(oauth.scopes),
            "ttl": int(time.time()) + 600,
        }
        if code_verifier:
            item["code_verifier"] = code_verifier
        self._table.put_item(Item=item)

        # Build the URL
        params: dict[str, str] = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": self._callback_url,
            "scope": " ".join(oauth.scopes),
            "state": state,
        }
        if oauth.uses_pkce and code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        params.update(oauth.extra_auth_params)

        auth_endpoint = self.resolve_endpoint(oauth.auth_endpoint, config)
        return f"{auth_endpoint}?{urlencode(params)}"

    # ------------------------------------------------------------------ #
    # Token exchange and refresh (pure HTTP — no service SDKs)            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def exchange_code(
        token_endpoint: str,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str | None = None,
        code_verifier: str | None = None,
    ) -> dict:
        """Standard OAuth2 authorization code exchange."""
        import httpx

        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if code_verifier:
            data["code_verifier"] = code_verifier

        with httpx.Client(timeout=30) as client:
            resp = client.post(token_endpoint, data=data)
            resp.raise_for_status()
            tokens = resp.json()

        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
            "token_type": tokens.get("token_type", "Bearer"),
            "scope": tokens.get("scope", ""),
        }

    @staticmethod
    def refresh_token(
        token_endpoint: str,
        refresh_token: str,
        client_id: str,
        client_secret: str | None = None,
        scopes: list[str] | None = None,
    ) -> dict:
        """Standard OAuth2 refresh token grant."""
        import httpx

        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        if scopes:
            data["scope"] = " ".join(scopes)

        with httpx.Client(timeout=30) as client:
            resp = client.post(token_endpoint, data=data)
            resp.raise_for_status()
            tokens = resp.json()

        return {
            "access_token": tokens["access_token"],
            # Some providers return a new refresh token; keep the old one if not
            "refresh_token": tokens.get("refresh_token", refresh_token),
            "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
            "token_type": tokens.get("token_type", "Bearer"),
            "scope": tokens.get("scope", ""),
        }

    # ------------------------------------------------------------------ #
    # Endpoint resolution                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve_endpoint(template: str, config: ServiceConfig) -> str:
        """Replace ``{placeholder}`` tokens in endpoint URLs from env vars."""
        if not config.oauth:
            return template
        result = template
        for placeholder, env_var in config.oauth.endpoint_params.items():
            value = os.environ.get(env_var, "")
            if value:
                result = result.replace(f"{{{placeholder}}}", value)
        return result
