"""Generic OAuth callback Lambda — handles authorization code exchange for all providers.

Receives the redirect from any external OAuth provider after user approval.
Reads pending state from DynamoDB, exchanges the code using standard OAuth2,
and stores the resulting tokens in Secrets Manager keyed by Cognito user.

Environment variables (set by CDK):
- OAUTH_STATE_TABLE: DynamoDB table for pending OAuth flows
- OAUTH_CALLBACK_URL: This callback endpoint's URL (used as redirect_uri)
- SECRET_PREFIX: Prefix for Secrets Manager secret names
"""

from __future__ import annotations

import json
import os
import time

import boto3
import httpx

secrets_client = boto3.client("secretsmanager")
dynamodb = boto3.resource("dynamodb")

OAUTH_STATE_TABLE = os.environ.get("OAUTH_STATE_TABLE", "")
OAUTH_CALLBACK_URL = os.environ.get("OAUTH_CALLBACK_URL", "")
SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "mcp-wrappers")


def handler(event, context):
    """API Gateway Lambda proxy handler."""
    if isinstance(event, dict) and (event.get("ping") or event.get("health")):
        return {"status": "ok", "handler": "oauth_callback"}

    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "GET" and path == "/oauth/callback":
        return _handle_callback(event)

    return _json_response(404, {"error": "not_found"})


def _handle_callback(event):
    """Exchange authorization code for tokens and store per-user."""
    params = event.get("queryStringParameters", {}) or {}
    code = params.get("code")
    state = params.get("state")
    error = params.get("error")

    if error:
        desc = params.get("error_description", error)
        return _html_response(400, f"Authentication failed: {desc}")

    if not code or not state:
        return _html_response(400, "Missing code or state parameter.")

    # ---- Look up pending state from DynamoDB ----
    table = dynamodb.Table(OAUTH_STATE_TABLE)
    resp = table.get_item(Key={"state": state})
    item = resp.get("Item")

    if not item:
        return _html_response(
            400, "Invalid or expired session. Please try authenticating again."
        )

    if item.get("ttl", 0) < time.time():
        table.delete_item(Key={"state": state})
        return _html_response(
            400, "Authentication session expired. Please try again."
        )

    # ---- Resolve client_secret from service secrets ----
    client_secret = ""
    service_secret_name = item.get("service_secret_name", "")
    client_secret_key = item.get("client_secret_key", "")
    if service_secret_name and client_secret_key:
        try:
            sec_resp = secrets_client.get_secret_value(SecretId=service_secret_name)
            service_secrets = json.loads(sec_resp.get("SecretString", "{}"))
            client_secret = service_secrets.get(client_secret_key, "")
        except Exception:
            pass

    # ---- Standard OAuth2 authorization code exchange ----
    token_endpoint = item["token_endpoint"]
    client_id = item.get("client_id", "")
    code_verifier = item.get("code_verifier")

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": OAUTH_CALLBACK_URL,
        "client_id": client_id,
    }
    if client_secret:
        token_data["client_secret"] = client_secret
    if code_verifier:
        token_data["code_verifier"] = code_verifier

    try:
        with httpx.Client(timeout=30) as http_client:
            token_resp = http_client.post(token_endpoint, data=token_data)
            token_resp.raise_for_status()
            tokens = token_resp.json()
    except httpx.HTTPStatusError as exc:
        detail = "Token exchange failed"
        try:
            err = exc.response.json()
            detail = err.get("error_description", err.get("error", detail))
        except Exception:
            pass
        return _html_response(500, f"Token exchange failed: {detail}")
    except Exception as exc:
        return _html_response(500, f"Token exchange failed: {exc}")

    # ---- Store credentials in Secrets Manager (per-user, per-service) ----
    user_id = item["user_id"]
    service_name = item["service_name"]
    secret_name = f"{SECRET_PREFIX}-{service_name}-user-{user_id}"

    creds = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
        "token_type": tokens.get("token_type", "Bearer"),
        "scope": tokens.get("scope", ""),
    }
    creds_json = json.dumps(creds)

    try:
        secrets_client.put_secret_value(
            SecretId=secret_name, SecretString=creds_json
        )
    except secrets_client.exceptions.ResourceNotFoundException:
        secrets_client.create_secret(Name=secret_name, SecretString=creds_json)

    # ---- Clean up state ----
    table.delete_item(Key={"state": state})

    return _html_response(
        200,
        "Authentication successful! You can close this window and return "
        "to your assistant.",
    )


def _html_response(status_code, message):
    success = status_code == 200
    color = "#22c55e" if success else "#ef4444"
    icon = "&#10003;" if success else "&#10007;"
    html = f"""<!DOCTYPE html>
<html><head><title>MCP Authentication</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       display: flex; justify-content: center; align-items: center;
       min-height: 100vh; margin: 0; background: #f5f5f5; }}
.card {{ background: white; padding: 2rem 3rem; border-radius: 12px;
         box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center;
         max-width: 420px; }}
.icon {{ font-size: 2.5rem; color: {color}; }}
p {{ color: #374151; line-height: 1.6; }}
</style></head>
<body><div class="card">
<div class="icon">{icon}</div>
<p>{message}</p>
</div></body></html>"""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }


def _json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
