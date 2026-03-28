"""AgentCore Gateway request interceptor — injects Cognito identity into tool args.

AgentCore Gateway validates the JWT but does not forward claims to
Lambda targets. This interceptor reads the Authorization header,
decodes the JWT payload (no verification needed — gateway already
validated it), and adds ``_cognito_sub`` to the MCP request body
so the target Lambda knows who the caller is.

Requires ``passRequestHeaders: true`` in the interceptor configuration.
"""

from __future__ import annotations

import base64
import json
import sys


def handler(event, context):
    """Intercept gateway request and inject Cognito sub."""
    try:
        mcp = event.get("mcp", {})
        request = mcp.get("gatewayRequest", {})
        headers = request.get("headers", {})
        body = request.get("body", {})

        # Extract JWT from Authorization header
        auth_header = headers.get("Authorization", headers.get("authorization", ""))
        cognito_sub = None

        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            # JWT is header.payload.signature — decode the payload
            parts = token.split(".")
            if len(parts) >= 2:
                # Add padding for base64
                payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                cognito_sub = payload.get("sub")

        if cognito_sub and isinstance(body, dict):
            body["_cognito_sub"] = cognito_sub

        # Return the (possibly modified) request
        return {
            "mcp": {
                "transformedGatewayRequest": {
                    "body": body,
                }
            }
        }
    except Exception as exc:
        print(f"[interceptor] Warning: {exc}", file=sys.stderr)
        # On error, pass through unmodified
        return {
            "mcp": {
                "transformedGatewayRequest": {
                    "body": event.get("mcp", {}).get("gatewayRequest", {}).get("body", {}),
                }
            }
        }
