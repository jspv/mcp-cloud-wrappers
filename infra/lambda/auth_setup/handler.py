"""Auth setup Lambda — web page for connecting external OAuth services.

Routes:
  GET /auth/setup               → Redirect to Cognito hosted UI login
  GET /auth/callback            → Exchange Cognito code, show service page
  GET /auth/connect/{service}   → Start external OAuth for a service
  GET /auth/status              → JSON connection status (for CLI)

Environment variables (set by CDK):
  COGNITO_DOMAIN          — Cognito hosted UI domain
  COGNITO_CLIENT_ID       — Auth setup app client ID
  COGNITO_USER_POOL_ID    — For DescribeUserPoolClient (to get client secret)
  AUTH_CALLBACK_URL       — This Lambda's callback URL (/auth/callback)
  AUTH_SETUP_URL          — This Lambda's setup URL (/auth/setup)
  OAUTH_CALLBACK_URL      — External OAuth callback URL (/oauth/callback)
  OAUTH_STATE_TABLE       — DynamoDB table for pending OAuth flows + sessions
  SECRET_PREFIX           — Prefix for Secrets Manager names
  SERVICE_OAUTH_CONFIGS   — JSON array of service OAuth configurations
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

import boto3
import httpx
import jwt

dynamodb = boto3.resource("dynamodb")
secrets_client = boto3.client("secretsmanager")

COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
AUTH_CALLBACK_URL = os.environ.get("AUTH_CALLBACK_URL", "")
AUTH_SETUP_URL = os.environ.get("AUTH_SETUP_URL", "")
OAUTH_CALLBACK_URL = os.environ.get("OAUTH_CALLBACK_URL", "")
OAUTH_STATE_TABLE = os.environ.get("OAUTH_STATE_TABLE", "")
SECRET_PREFIX = os.environ.get("SECRET_PREFIX", "mcp-wrappers")
SERVICE_CONFIGS = json.loads(os.environ.get("SERVICE_OAUTH_CONFIGS", "[]"))

# Resolve Cognito client secret at cold start
_cognito_client_secret = ""
try:
    _cog = boto3.client("cognito-idp")
    _resp = _cog.describe_user_pool_client(
        UserPoolId=COGNITO_USER_POOL_ID, ClientId=COGNITO_CLIENT_ID,
    )
    _cognito_client_secret = _resp["UserPoolClient"].get("ClientSecret", "")
except Exception as exc:
    print(f"[auth-setup] Warning: could not get Cognito client secret: {exc}",
          file=sys.stderr)


def handler(event, context):
    if isinstance(event, dict) and (event.get("ping") or event.get("health")):
        return {"status": "ok", "handler": "auth_setup"}

    method = event.get("httpMethod", "")
    path = event.get("path", "")

    if method == "GET" and path == "/auth/setup":
        return _handle_setup(event)
    elif method == "GET" and path == "/auth/callback":
        return _handle_callback(event)
    elif method == "GET" and path.startswith("/auth/connect/"):
        service = path.split("/auth/connect/", 1)[1].strip("/")
        return _handle_connect(event, service)
    elif method == "GET" and path == "/auth/status":
        return _handle_status(event)
    else:
        return _json_response(404, {"error": "not_found"})


def _handle_setup(event):
    params = event.get("queryStringParameters", {}) or {}
    session_token = params.get("session")
    connected = params.get("connected")
    if session_token:
        session = _load_session(session_token)
        if session:
            return _render_service_page(
                session["sub"], session["email"], connected=connected,
                session_token=session_token,
            )

    state = secrets.token_urlsafe(32)
    table = dynamodb.Table(OAUTH_STATE_TABLE)
    table.put_item(Item={
        "state": f"cognito#{state}",
        "flow_type": "cognito_login",
        "ttl": int(time.time()) + 600,
    })

    cognito_params = {
        "client_id": COGNITO_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": AUTH_CALLBACK_URL,
        "scope": "openid email",
        "state": state,
    }
    url = f"https://{COGNITO_DOMAIN}/oauth2/authorize?{urlencode(cognito_params)}"
    return {"statusCode": 302, "headers": {"Location": url}, "body": ""}


def _handle_callback(event):
    params = event.get("queryStringParameters", {}) or {}
    code = params.get("code")
    state = params.get("state")
    error = params.get("error")

    if error:
        return _html_page(400, "Login Failed",
                          f"Cognito login failed: {params.get('error_description', error)}")

    if not code or not state:
        return _html_page(400, "Missing Parameters", "Missing code or state.")

    table = dynamodb.Table(OAUTH_STATE_TABLE)
    resp = table.get_item(Key={"state": f"cognito#{state}"})
    item = resp.get("Item")
    if not item:
        return _html_page(400, "Invalid Session", "Session expired. Please try again.")
    table.delete_item(Key={"state": f"cognito#{state}"})

    token_url = f"https://{COGNITO_DOMAIN}/oauth2/token"
    auth_header = base64.b64encode(
        f"{COGNITO_CLIENT_ID}:{_cognito_client_secret}".encode()
    ).decode()

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(token_url, data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": AUTH_CALLBACK_URL,
            }, headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            resp.raise_for_status()
            tokens = resp.json()
    except Exception as exc:
        return _html_page(500, "Login Failed", f"Could not complete login: {exc}")

    try:
        claims = jwt.decode(tokens["id_token"], options={"verify_signature": False})
        cognito_sub = claims.get("sub", "")
        email = claims.get("email", cognito_sub)
    except Exception as exc:
        return _html_page(500, "Login Failed", f"Could not read identity: {exc}")

    session_token = _create_session(cognito_sub, email)
    return _render_service_page(cognito_sub, email, session_token=session_token)


def _handle_connect(event, service_name):
    params = event.get("queryStringParameters", {}) or {}
    session_token = params.get("session")

    if not session_token:
        return _html_page(400, "Not Logged In", "Please visit /auth/setup first.")

    session = _load_session(session_token)
    if not session:
        return _html_page(400, "Session Expired", "Please visit /auth/setup to log in again.")

    cognito_sub = session["sub"]
    email = session["email"]

    svc_config = None
    for cfg in SERVICE_CONFIGS:
        if cfg["service_name"] == service_name:
            svc_config = cfg
            break
    if not svc_config:
        return _html_page(404, "Service Not Found",
                          f"No OAuth configuration for '{service_name}'.")

    auth_endpoint = svc_config["auth_endpoint"]
    for placeholder, env_key in svc_config.get("endpoint_params", {}).items():
        value = svc_config.get("resolved_env", {}).get(env_key, "")
        auth_endpoint = auth_endpoint.replace(f"{{{placeholder}}}", value)

    token_endpoint = svc_config["token_endpoint"]
    for placeholder, env_key in svc_config.get("endpoint_params", {}).items():
        value = svc_config.get("resolved_env", {}).get(env_key, "")
        token_endpoint = token_endpoint.replace(f"{{{placeholder}}}", value)

    client_id = ""
    secret_name = svc_config.get("service_secret_name", "")
    client_id_key = svc_config.get("client_id_key", "")
    if secret_name and client_id_key:
        try:
            sec_resp = secrets_client.get_secret_value(SecretId=secret_name)
            svc_secrets = json.loads(sec_resp.get("SecretString", "{}"))
            client_id = svc_secrets.get(client_id_key, "")
        except Exception:
            pass

    if not client_id:
        return _html_page(500, "Configuration Error",
                          f"Could not resolve client_id for {service_name}.")

    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    return_url = f"{AUTH_SETUP_URL}?session={session_token}"

    oauth_state = secrets.token_urlsafe(32)
    table = dynamodb.Table(OAUTH_STATE_TABLE)
    table.put_item(Item={
        "state": oauth_state,
        "user_id": cognito_sub,
        "service_name": service_name,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "client_secret_key": svc_config.get("client_secret_key", ""),
        "service_secret_name": secret_name,
        "scopes": " ".join(svc_config.get("scopes", [])),
        "code_verifier": code_verifier,
        "return_url": return_url,
        "ttl": int(time.time()) + 600,
    })

    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": OAUTH_CALLBACK_URL,
        "scope": " ".join(svc_config.get("scopes", [])),
        "state": oauth_state,
    }
    if svc_config.get("uses_pkce", True):
        auth_params["code_challenge"] = code_challenge
        auth_params["code_challenge_method"] = "S256"

    return {
        "statusCode": 302,
        "headers": {"Location": f"{auth_endpoint}?{urlencode(auth_params)}"},
        "body": "",
    }


def _handle_status(event):
    params = event.get("queryStringParameters", {}) or {}
    session_token = params.get("session")

    if not session_token:
        return _json_response(400, {"error": "session parameter required"})

    session = _load_session(session_token)
    if not session:
        return _json_response(400, {"error": "session expired"})

    cognito_sub = session["sub"]
    statuses = {}
    for cfg in SERVICE_CONFIGS:
        name = cfg["service_name"]
        secret_name = f"{SECRET_PREFIX}-{name}-user-{cognito_sub}"
        try:
            secrets_client.get_secret_value(SecretId=secret_name)
            statuses[name] = "connected"
        except secrets_client.exceptions.ResourceNotFoundException:
            statuses[name] = "not_connected"
        except Exception:
            statuses[name] = "unknown"

    return _json_response(200, {"user_id": cognito_sub, "services": statuses})


def _create_session(cognito_sub, email):
    session_token = secrets.token_urlsafe(32)
    table = dynamodb.Table(OAUTH_STATE_TABLE)
    table.put_item(Item={
        "state": f"session#{session_token}",
        "sub": cognito_sub,
        "email": email,
        "ttl": int(time.time()) + 600,
    })
    return session_token


def _load_session(session_token):
    table = dynamodb.Table(OAUTH_STATE_TABLE)
    resp = table.get_item(Key={"state": f"session#{session_token}"})
    item = resp.get("Item")
    if not item:
        return None
    if item.get("ttl", 0) < time.time():
        table.delete_item(Key={"state": f"session#{session_token}"})
        return None
    return {"sub": item["sub"], "email": item["email"]}


def _render_service_page(cognito_sub, email, connected=None, session_token=""):
    cards = []
    for cfg in SERVICE_CONFIGS:
        name = cfg["service_name"]
        display = cfg.get("display_name", name)
        secret_name = f"{SECRET_PREFIX}-{name}-user-{cognito_sub}"
        is_connected = False
        try:
            secrets_client.get_secret_value(SecretId=secret_name)
            is_connected = True
        except Exception:
            pass

        just_connected = (connected == name)
        if is_connected or just_connected:
            status_html = '<span class="status connected">Connected</span>'
            button_html = ""
        else:
            status_html = '<span class="status disconnected">Not connected</span>'
            # Use full URL — relative paths lose the /prod stage prefix
            base = AUTH_SETUP_URL.rsplit("/auth/setup", 1)[0] if AUTH_SETUP_URL else ""
            connect_url = f"{base}/auth/connect/{name}?session={session_token}"
            button_html = f'<a href="{connect_url}" class="btn">Connect</a>'

        cards.append(f"""
        <div class="card{' highlight' if just_connected else ''}">
            <h2>{display}</h2>
            {status_html}
            {button_html}
        </div>""")

    flash = ""
    if connected:
        display = connected
        for cfg in SERVICE_CONFIGS:
            if cfg["service_name"] == connected:
                display = cfg.get("display_name", connected)
        flash = f'<div class="flash">{display} connected successfully!</div>'

    html = f"""<!DOCTYPE html>
<html><head><title>MCP Service Connections</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 600px; margin: 40px auto; padding: 0 20px;
       background: #f9fafb; color: #111827; }}
h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
.subtitle {{ color: #6b7280; margin-bottom: 24px; }}
.flash {{ background: #dcfce7; border: 1px solid #86efac; border-radius: 8px;
          padding: 12px 16px; margin-bottom: 16px; color: #166534; }}
.card {{ background: white; border-radius: 8px; padding: 16px 20px;
         box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 12px;
         display: flex; align-items: center; gap: 16px; }}
.card.highlight {{ border: 2px solid #86efac; }}
.card h2 {{ font-size: 1.1rem; margin: 0; flex: 1; }}
.status {{ font-size: 0.875rem; padding: 4px 10px; border-radius: 12px; }}
.status.connected {{ background: #dcfce7; color: #166534; }}
.status.disconnected {{ background: #fee2e2; color: #991b1b; }}
.btn {{ background: #2563eb; color: white; text-decoration: none;
        padding: 8px 16px; border-radius: 6px; font-size: 0.875rem; }}
.btn:hover {{ background: #1d4ed8; }}
.empty {{ color: #6b7280; margin-top: 24px; }}
</style></head>
<body>
<h1>MCP Service Connections</h1>
<p class="subtitle">Signed in as {email}</p>
{flash}
{''.join(cards)}
{'<p class="empty">No services with OAuth configured.</p>' if not cards else ''}
</body></html>"""

    return {"statusCode": 200, "headers": {"Content-Type": "text/html"}, "body": html}


def _html_page(status_code, title, message):
    color = "#22c55e" if status_code == 200 else "#ef4444"
    html = f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       display: flex; justify-content: center; align-items: center;
       min-height: 100vh; margin: 0; background: #f5f5f5; }}
.card {{ background: white; padding: 2rem 3rem; border-radius: 12px;
         box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center; max-width: 420px; }}
h1 {{ color: {color}; font-size: 1.3rem; }}
p {{ color: #374151; line-height: 1.6; }}
a {{ color: #2563eb; }}
</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p>
<p><a href="{AUTH_SETUP_URL or '/auth/setup'}">Back to setup</a></p>
</div></body></html>"""
    return {"statusCode": status_code, "headers": {"Content-Type": "text/html"}, "body": html}


def _json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
