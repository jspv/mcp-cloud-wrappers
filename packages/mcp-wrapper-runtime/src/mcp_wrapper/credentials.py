"""Credential manager backed by AWS Secrets Manager.

Handles three operations:
- Loading service-level secrets (Category 2: shared across all users)
- Loading per-user credentials (Category 3: OAuth tokens per Cognito user)
- Storing/updating per-user credentials after token exchange or refresh
"""

from __future__ import annotations

import json
import os
import sys


class CredentialManager:
    """Manages credentials in AWS Secrets Manager."""

    def __init__(self, prefix: str | None = None) -> None:
        self._prefix = prefix or os.environ.get("SECRET_PREFIX", "mcp-wrappers")
        self._client = None

    @property
    def _secrets(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("secretsmanager")
        return self._client

    def load_service_secrets(self, secret_name: str) -> dict[str, str]:
        """Load Category 2 service-level secrets.

        Returns a dict of env-var-name -> secret-value.  The secret's JSON
        keys become subprocess environment variable names.
        """
        resolved = secret_name.replace("{prefix}", self._prefix)
        try:
            resp = self._secrets.get_secret_value(SecretId=resolved)
            return json.loads(resp.get("SecretString", "{}"))
        except self._secrets.exceptions.ResourceNotFoundException:
            return {}
        except Exception as exc:
            print(
                f"[mcp-wrapper] Warning: failed to load service secrets "
                f"'{resolved}': {exc}",
                file=sys.stderr,
            )
            return {}

    def load_user_credentials(
        self, user_id: str, service_name: str
    ) -> dict | None:
        """Load Category 3 per-user OAuth credentials.

        Returns the stored token dict or None if the user hasn't
        authenticated yet.
        """
        secret_name = f"{self._prefix}-{service_name}-user-{user_id}"
        try:
            resp = self._secrets.get_secret_value(SecretId=secret_name)
            return json.loads(resp.get("SecretString", "{}"))
        except self._secrets.exceptions.ResourceNotFoundException:
            return None
        except Exception as exc:
            print(
                f"[mcp-wrapper] Warning: failed to load user credentials "
                f"for {service_name}/{user_id}: {exc}",
                file=sys.stderr,
            )
            return None

    def store_user_credentials(
        self, user_id: str, service_name: str, creds: dict
    ) -> None:
        """Store or update Category 3 per-user OAuth credentials."""
        secret_name = f"{self._prefix}-{service_name}-user-{user_id}"
        secret_string = json.dumps(creds)
        try:
            self._secrets.put_secret_value(
                SecretId=secret_name, SecretString=secret_string
            )
        except self._secrets.exceptions.ResourceNotFoundException:
            self._secrets.create_secret(
                Name=secret_name, SecretString=secret_string
            )
        except Exception as exc:
            print(
                f"[mcp-wrapper] Warning: failed to store user credentials "
                f"for {service_name}/{user_id}: {exc}",
                file=sys.stderr,
            )
