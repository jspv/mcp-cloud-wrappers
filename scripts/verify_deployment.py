#!/usr/bin/env python3
"""Smoke test for deployed MCP Lambda wrapper infrastructure.

Usage:
  python scripts/verify_deployment.py \\
    --dcr-api-url https://xxx.execute-api.us-east-1.amazonaws.com/prod/ \\
    --gateway-url https://xxx.bedrock-agentcore.us-east-1.amazonaws.com/
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx


def check_dcr_metadata(dcr_api_url: str) -> bool:
    """Verify OIDC discovery endpoint returns valid metadata."""
    url = f"{dcr_api_url.rstrip('/')}/.well-known/openid-configuration"
    print(f"  GET {url}")
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        required = ["issuer", "authorization_endpoint", "token_endpoint", "registration_endpoint"]
        missing = [k for k in required if k not in data]
        if missing:
            print(f"  FAIL: missing keys: {missing}")
            return False
        print(f"  OK: issuer={data['issuer']}")
        return True
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def check_oauth_callback(dcr_api_url: str) -> bool:
    """Verify OAuth callback endpoint exists (returns error without params, but 200 route)."""
    url = f"{dcr_api_url.rstrip('/')}/oauth/callback"
    print(f"  GET {url}")
    try:
        resp = httpx.get(url, timeout=10)
        # Should return 400 (missing params) not 404 (route not found)
        if resp.status_code == 400:
            print("  OK: callback endpoint exists (returned 400 — expected without params)")
            return True
        elif resp.status_code == 404:
            print("  FAIL: callback route not found (404)")
            return False
        else:
            print(f"  OK: callback endpoint returned {resp.status_code}")
            return True
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def check_dcr_register(dcr_api_url: str) -> bool:
    """Test dynamic client registration with a probe request."""
    url = f"{dcr_api_url.rstrip('/')}/register"
    print(f"  POST {url}")
    try:
        resp = httpx.post(
            url,
            json={
                "client_name": "verify-deployment-probe",
                "redirect_uris": ["http://localhost:9999/callback"],
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            print(f"  OK: client_id={data.get('client_id', '?')}")
            return True
        else:
            print(f"  WARN: status={resp.status_code} body={resp.text[:200]}")
            return False
    except Exception as exc:
        print(f"  FAIL: {exc}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Verify MCP Lambda wrapper deployment")
    parser.add_argument("--dcr-api-url", required=True, help="DCR API Gateway URL")
    parser.add_argument("--gateway-url", help="AgentCore Gateway URL (optional)")
    args = parser.parse_args()

    checks = [
        ("OIDC Discovery", lambda: check_dcr_metadata(args.dcr_api_url)),
        ("OAuth Callback Route", lambda: check_oauth_callback(args.dcr_api_url)),
        ("DCR Registration", lambda: check_dcr_register(args.dcr_api_url)),
    ]

    results = []
    for name, check in checks:
        print(f"\n[{name}]")
        results.append((name, check()))

    print("\n--- Summary ---")
    all_ok = True
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {name}")
        if not ok:
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
