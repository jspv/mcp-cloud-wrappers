#!/usr/bin/env python3
"""Open the auth setup page in the default browser.

Usage:
  make auth
"""

from __future__ import annotations

import json
import subprocess
import sys
import webbrowser


def main():
    prefix = "mcp-wrappers"
    stack_name = f"{prefix}-shared"

    try:
        result = subprocess.run(
            ["aws", "cloudformation", "describe-stacks",
             "--stack-name", stack_name,
             "--query", "Stacks[0].Outputs", "--output", "json"],
            capture_output=True, text=True, check=True,
        )
        outputs = json.loads(result.stdout)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("Deploy the shared stack first: make deploy-shared", file=sys.stderr)
        sys.exit(1)

    auth_url = None
    dcr_url = None
    for output in outputs or []:
        key = output.get("OutputKey", "")
        value = output.get("OutputValue", "")
        if "AuthSetupUrl" in key:
            auth_url = value
        if "DcrApiUrl" in key:
            dcr_url = value

    if not auth_url and dcr_url:
        auth_url = f"{dcr_url.rstrip('/')}/auth/setup"

    if not auth_url:
        print("Error: AuthSetupUrl not found in stack outputs.", file=sys.stderr)
        sys.exit(1)

    print(f"Opening: {auth_url}")
    webbrowser.open(auth_url)


if __name__ == "__main__":
    main()
