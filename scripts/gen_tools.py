#!/usr/bin/env python3
"""Generate tools.json for a service by introspecting its MCP server.

Usage:
  make gen-tools SERVICE=msgraph

Finds the MCP server package location from requirements.local.txt (or
requirements.txt), runs `uv run` inside that project to introspect its
FastMCP tools, and writes tools.json to the service directory.

For non-Python MCP servers, tools.json must be maintained manually.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from urllib.parse import urlparse


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


def _find_mcp_pkg_dir(service_dir: str) -> str | None:
    """Find the MCP server package directory from requirements.txt.

    Looks for a ``file://`` path.  Returns the local path or None.
    """
    for filename in ("requirements.txt",):
        req_path = os.path.join(service_dir, filename)
        if not os.path.isfile(req_path):
            continue
        with open(req_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Match: package-name @ file:///path/to/dir
                match = re.search(r"@\s*file://(/\S+)", line)
                if match:
                    path = match.group(1)
                    # Skip the framework runtime package
                    if "mcp-wrapper-runtime" in path:
                        continue
                    if os.path.isdir(path):
                        return path
    return None


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

def simplify_prop(prop):
    """Simplify a JSON Schema property for CDK compatibility.

    CDK SchemaDefinitionProperty requires a flat {"type": "..."} for each
    property.  FastMCP generates anyOf unions for Optional types like
    {"anyOf": [{"type": "string"}, {"type": "null"}]}.  We flatten these
    to the first non-null type.
    """
    if "anyOf" in prop:
        for variant in prop["anyOf"]:
            if variant.get("type") != "null":
                simple = {"type": variant["type"]}
                # Preserve items for array types
                if variant.get("type") == "array" and "items" in variant:
                    simple["items"] = variant["items"]
                if "default" in prop:
                    simple["default"] = prop["default"]
                return simple
    # Ensure arrays always have items
    if prop.get("type") == "array" and "items" not in prop:
        prop = {**prop, "items": {"type": "string"}}
    if "type" not in prop:
        return {"type": "string"}
    return prop

def simplify_properties(properties):
    return {k: simplify_prop(v) for k, v in properties.items()}

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
                "properties": simplify_properties(schema["properties"]),
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
        print("Usage: make gen-tools SERVICE=<name>", file=sys.stderr)
        sys.exit(1)

    service_name = sys.argv[1]

    services_dir = os.path.join(
        os.path.dirname(__file__), "..", "infra", "lambda", "services"
    )
    service_dir = os.path.normpath(os.path.join(services_dir, service_name))

    if not os.path.isdir(service_dir):
        print(f"Error: service directory not found: {service_dir}", file=sys.stderr)
        sys.exit(1)

    mcp_module = _find_mcp_module(service_dir)

    # Find MCP server package directory from requirements files,
    # or accept it as a CLI arg / env var override.
    mcp_pkg_dir = (
        (sys.argv[2] if len(sys.argv) > 2 else None)
        or os.environ.get("MCP_PKG_DIR")
        or _find_mcp_pkg_dir(service_dir)
    )

    if not mcp_pkg_dir:
        print(
            f"Error: could not find MCP server package location.\n"
            f"Either add a file:// path in requirements.local.txt, or pass it:\n"
            f"  make gen-tools SERVICE={service_name} MCP_PKG_DIR=/path/to/repo",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.path.isdir(mcp_pkg_dir):
        print(f"Error: MCP package directory not found: {mcp_pkg_dir}", file=sys.stderr)
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

    MAX_TOOLS = 30
    if len(tools) > MAX_TOOLS:
        print(
            f"Error: MCP server exposes {len(tools)} tools, but AgentCore Gateway\n"
            f"returns at most {MAX_TOOLS} per page in tools/list responses. MCP clients\n"
            f"(Claude.ai, ChatGPT) do not paginate, so tools beyond {MAX_TOOLS} will be\n"
            f"invisible.\n"
            f"\n"
            f"Reduce the MCP server to {MAX_TOOLS} tools or fewer, then re-run.\n"
            f"\n"
            f"Tools found ({len(tools)}):",
            file=sys.stderr,
        )
        for i, t in enumerate(sorted(tools, key=lambda t: t["name"])):
            print(f"  {i+1}. {t['name']}", file=sys.stderr)
        sys.exit(1)

    out_path = os.path.join(service_dir, "tools.json")
    with open(out_path, "w") as f:
        json.dump(tools, f, indent=2)
        f.write("\n")

    print(f"Wrote {len(tools)} tools to {out_path}")


if __name__ == "__main__":
    main()
