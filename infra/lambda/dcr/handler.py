"""DCR bridge Lambda — RFC 7591 Dynamic Client Registration + OAuth metadata.

Serves three endpoints via API Gateway:
- GET /.well-known/openid-configuration  -> OIDC discovery (wraps Cognito + adds registration_endpoint)
- GET /.well-known/oauth-authorization-server -> same content, different RFC
- POST /register -> creates a Cognito UserPoolClient dynamically

Environment variables (set by CDK):
- USER_POOL_ID: Cognito User Pool ID
- HOSTED_UI_DOMAIN: Cognito Hosted UI domain (bare hostname)
- RESOURCE_SERVER_ID: Resource server identifier for scope names
- REGION: AWS region
- DCR_TABLE_NAME: DynamoDB table for storing registrations
- DCR_API_URL: This API Gateway's URL (for registration_endpoint in metadata)
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from urllib.parse import urlparse

import boto3

cognito = boto3.client("cognito-idp")
dynamodb = boto3.resource("dynamodb")

USER_POOL_ID = os.environ["USER_POOL_ID"]
HOSTED_UI_DOMAIN = os.environ["HOSTED_UI_DOMAIN"]
RESOURCE_SERVER_ID = os.environ["RESOURCE_SERVER_ID"]
REGION = os.environ["REGION"]
DCR_TABLE_NAME = os.environ["DCR_TABLE_NAME"]
DCR_API_URL = os.environ["DCR_API_URL"]

ALLOWED_SCOPES = [
    "openid",
    "email",
    f"{RESOURCE_SERVER_ID}/read",
    f"{RESOURCE_SERVER_ID}/write",
]


def handler(event, context):
    """API Gateway Lambda proxy handler."""
    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "GET" and path in (
        "/.well-known/openid-configuration",
        "/.well-known/oauth-authorization-server",
    ):
        return _metadata_response()
    elif method == "POST" and path == "/register":
        return _register(event)
    else:
        return _json_response(404, {"error": "not_found"})


def _metadata_response():
    """Return OIDC/OAuth authorization server metadata."""
    issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
    return _json_response(200, {
        "issuer": issuer,
        "authorization_endpoint": f"https://{HOSTED_UI_DOMAIN}/oauth2/authorize",
        "token_endpoint": f"https://{HOSTED_UI_DOMAIN}/oauth2/token",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "registration_endpoint": f"{DCR_API_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "scopes_supported": ALLOWED_SCOPES,
        "code_challenge_methods_supported": ["S256"],
        # Advertise that public (PKCE, no secret) clients are supported, per
        # RFC 8414. Cognito enforces PKCE automatically for public app clients
        # on the authorization-code grant.
        "token_endpoint_auth_methods_supported": [
            "none", "client_secret_basic", "client_secret_post",
        ],
    })


def _register(event):
    """RFC 7591 Dynamic Client Registration."""
    try:
        body = json.loads(event.get("body", "{}") or "{}")
    except json.JSONDecodeError:
        return _json_response(400, {"error": "invalid_client_metadata"})

    client_name = body.get("client_name", "")
    redirect_uris = body.get("redirect_uris", [])
    # RFC 7591: default is client_secret_basic when omitted. "none" requests a
    # public client (PKCE, no secret) — which is what native/loopback clients
    # such as Hermes Agent and most MCP clients use.
    auth_method = body.get("token_endpoint_auth_method", "client_secret_basic")
    is_public = auth_method == "none"

    # --- Validation ---
    if not client_name:
        return _json_response(400, {
            "error": "invalid_client_metadata",
            "error_description": "client_name is required",
        })
    if not redirect_uris or not isinstance(redirect_uris, list):
        return _json_response(400, {
            "error": "invalid_client_metadata",
            "error_description": "redirect_uris must be a non-empty list",
        })
    if len(redirect_uris) > 5:
        return _json_response(400, {
            "error": "invalid_client_metadata",
            "error_description": "maximum 5 redirect_uris",
        })
    for uri in redirect_uris:
        if "#" in uri:
            return _json_response(400, {
                "error": "invalid_client_metadata",
                "error_description": "redirect_uris must not contain fragments",
            })
        # Per RFC 8252 §7.3, native apps use a loopback redirect and the IP
        # literal (127.0.0.1 / ::1) is RECOMMENDED over the "localhost"
        # hostname. Allow plain http only for loopback hosts; require https
        # otherwise.
        parsed = urlparse(uri)
        is_loopback = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if not (parsed.scheme == "https" or (parsed.scheme == "http" and is_loopback)):
            return _json_response(400, {
                "error": "invalid_client_metadata",
                "error_description": (
                    "redirect_uris must use https, or http on a loopback host "
                    "(localhost, 127.0.0.1, or ::1)"
                ),
            })

    # --- Idempotency check ---
    uris_hash = hashlib.sha256(
        json.dumps(sorted(redirect_uris)).encode()
    ).hexdigest()[:16]

    table = dynamodb.Table(DCR_TABLE_NAME)
    resp = table.scan(
        FilterExpression="client_name = :cn AND redirect_uris_hash = :h",
        ExpressionAttributeValues={":cn": client_name, ":h": uris_hash},
    )
    if resp.get("Items"):
        existing = resp["Items"][0]
        try:
            client_info = cognito.describe_user_pool_client(
                UserPoolId=USER_POOL_ID,
                ClientId=existing["client_id"],
            )["UserPoolClient"]
            existing_secret = client_info.get("ClientSecret", "")
            # Prefer the stored flag; fall back to secret presence for rows
            # created before this field existed.
            existing_public = existing.get("public", not bool(existing_secret))
            return _json_response(200, {
                "client_id": existing["client_id"],
                "client_secret": "" if existing_public else existing_secret,
                "client_id_issued_at": int(existing.get("created_at", 0)),
                "client_secret_expires_at": 0,
                "redirect_uris": redirect_uris,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none" if existing_public else "client_secret_basic",
                "client_name": client_name,
            })
        except cognito.exceptions.ResourceNotFoundException:
            pass

    # --- Create Cognito UserPoolClient ---
    cognito_client_name = f"dcr-{client_name}-{uris_hash}"[:128]
    result = cognito.create_user_pool_client(
        UserPoolId=USER_POOL_ID,
        ClientName=cognito_client_name,
        # Honor the requested auth method. Public clients (token_endpoint_auth_
        # method=none) get no secret and rely on PKCE, which Cognito enforces
        # automatically for public app clients on the code grant.
        GenerateSecret=not is_public,
        AllowedOAuthFlows=["code"],
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthScopes=ALLOWED_SCOPES,
        CallbackURLs=redirect_uris,
        SupportedIdentityProviders=["COGNITO"],
    )
    client = result["UserPoolClient"]
    now = int(time.time())

    # --- Store in DynamoDB ---
    table.put_item(Item={
        "client_id": client["ClientId"],
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "redirect_uris_hash": uris_hash,
        "created_at": now,
        "cognito_client_name": cognito_client_name,
        "public": is_public,
    })

    return _json_response(201, {
        "client_id": client["ClientId"],
        "client_secret": "" if is_public else client.get("ClientSecret", ""),
        "client_id_issued_at": now,
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none" if is_public else "client_secret_basic",
        "client_name": client_name,
    })


def _json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }
