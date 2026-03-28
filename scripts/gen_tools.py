#!/usr/bin/env python3
"""Generate tools.json for a service by introspecting its MCP server.

Usage:
  make gen-tools SERVICE=msgraph MCP_PKG_DIR=/path/to/msgraph-email-calendar-mcp

Runs `uv run` inside the MCP server's project directory to introspect
its FastMCP tools, then writes tools.json to the service directory in
this repo.

For non-Python MCP servers, tools.json must be maintained manually.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def _find_mcp_module(service_dir: str) -> str:
    """Parse mcp_module from handler.py."""
    handler_path = os.path.join(service_dir, "handler.py")
    with open(handler_path) as f:
        content = f.read()
    match = re.search(r'mcp_module\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        print(f"Error: could not find mcp_module in {handler_path}", file=sys.stderr)
        sys.exit(1)
    return match.group(1)


_INTROSPECT_SCRIPT = '''
import asyncio, importlib, json, sys

def find_fastmcp(mod):
    for name in ("mcp", "app", "server"):
        obj = getattr(mod, name, None)
        if obj and hasattr(obj, "list_tools") and hasattr(obj, "tool"):
            return obj
    for name in dir(mod):
        obj = getattr(mod, name, None)
        if obj and hasattr(obj, "list_tools") and hasattr(obj, "tool"):
            return obj
    return None

async def dump(module_name):
    parent = module_name.rsplit(".", 1)[0] if "." in module_name else module_name
    fastmcp = None
    for candidate in (f"{parent}.tools", module_name, parent):
        try:
            mod = importlib.import_module(candidate)
            fastmcp = find_fastmcp(mod)
            if fastmcp:
                break
        except ImportError:
            continue
    if not fastmcp:
        print(f"Error: no FastMCP instance found", file=sys.stderr)
        sys.exit(1)

    tools = await fastmcp.list_tools()
    result = []
    for t in tools:
        mcp_tool = t.to_mcp_tool()
        entry = {"name": mcp_tool.name, "description": mcp_tool.description or ""}
        schema = mcp_tool.inputSchema
        if schema and schema.get("properties"):
            entry["inputSchema"] = {
                "type": schema.get("type", "object"),
                "properties": schema.get("properties", {}),
            }
            req = schema.get("required", [])
            if req:
                entry["inputSchema"]["required"] = req
        result.append(entry)
    print(json.dumps(result, indent=2))

asyncio.run(dump(sys.argv[1]))
'''


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: make gen-tools SERVICE=<name> MCP_PKG_DIR=/path/to/mcp-server-repo",
            file=sys.stderr,
        )
        sys.exit(1)

    service_name = sys.argv[1]
    mcp_pkg_dir = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("MCP_PKG_DIR", "")

    services_dir = os.path.join(
        os.path.dirname(__file__), "..", "infra", "lambda", "services"
    )
    service_dir = os.path.normpath(os.path.join(services_dir, service_name))

    if not os.path.isdir(service_dir):
        print(f"Error: service directory not found: {service_dir}", file=sys.stderr)
        sys.exit(1)

    mcp_module = _find_mcp_module(service_dir)

    if not mcp_pkg_dir:
        print(
            f"Error: MCP_PKG_DIR not set. Point it at the MCP server's project directory.\n"
            f"  make gen-tools SERVICE={service_name} MCP_PKG_DIR=/path/to/repo",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.path.isdir(mcp_pkg_dir):
        print(f"Error: MCP_PKG_DIR not found: {mcp_pkg_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Introspecting {mcp_module} from {mcp_pkg_dir}...")

    # Run the introspection script inside the MCP server's project (using its venv).
    result = subprocess.run(
        ["uv", "run", "python", "-c", _INTROSPECT_SCRIPT, mcp_module],
        cwd=mcp_pkg_dir,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error introspecting tools:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Validate and write.
    tools = json.loads(result.stdout)
    out_path = os.path.join(service_dir, "tools.json")
    with open(out_path, "w") as f:
        json.dump(tools, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(tools)} tools to {out_path}")


if __name__ == "__main__":
    main()
