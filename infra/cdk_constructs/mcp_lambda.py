"""MCP Server Lambda construct — creates a per-service Lambda function.

The Lambda runs the McpServiceHandler which launches the MCP server
as a stdio subprocess via the mcp_lambda adapter.
"""

from __future__ import annotations

import os
import re

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    aws_iam as iam,
    aws_lambda as lambda_,
)
from constructs import Construct

from .bundler import LocalPipBundler

# mcp-wrapper-runtime lives at a known location relative to this file.
_RUNTIME_PKG = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "packages", "mcp-wrapper-runtime")
)


def _docker_volumes(service_dir: str) -> list[cdk.DockerVolume]:
    """Build Docker volume mounts for container bundling.

    Mounts mcp-wrapper-runtime and any file:// paths from requirements.txt
    so pip inside the container can resolve them.
    """
    volumes = [
        cdk.DockerVolume(
            host_path=_RUNTIME_PKG,
            container_path="/mcp-wrapper-runtime",
        ),
    ]
    # Find file:// paths in requirements.txt and mount them
    req = os.path.join(service_dir, "requirements.txt")
    if os.path.isfile(req):
        with open(req) as f:
            for line in f:
                match = re.search(r"file://(/\S+)", line.strip())
                if match:
                    host_path = match.group(1)
                    if os.path.isdir(host_path):
                        # Mount at the same path so the file:// URI works as-is
                        volumes.append(
                            cdk.DockerVolume(
                                host_path=host_path,
                                container_path=host_path,
                            )
                        )
    return volumes


class McpServerLambda(Construct):
    """Lambda function for a specific MCP service."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        service_name: str,
        handler_source_dir: str,
        timeout_seconds: int = 120,
        memory_mb: int = 512,
        environment: dict[str, str] | None = None,
        extra_policies: list[iam.PolicyStatement] | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        # ---- IAM role ----
        self.role = iam.Role(
            self,
            "McpLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        for policy in extra_policies or []:
            self.role.add_to_policy(policy)

        # ---- Lambda function ----
        self.function = lambda_.Function(
            self,
            "McpServerLambda",
            function_name=f"{prefix}-{service_name}-mcp",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                handler_source_dir,
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=LocalPipBundler(handler_source_dir),
                    # Docker/Podman fallback for compiled dependencies.
                    # Mounts mcp-wrapper-runtime and any file:// paths
                    # from requirements.txt at their original paths so
                    # pip resolves them identically inside the container.
                    volumes=_docker_volumes(handler_source_dir),
                    command=[
                        "bash", "-c",
                        "pip install --no-cache-dir /mcp-wrapper-runtime -t /asset-output && "
                        "pip install --no-cache-dir -r requirements.txt -t /asset-output && "
                        "cp -au . /asset-output",
                    ],
                ),
            ),
            role=self.role,
            timeout=Duration.seconds(timeout_seconds),
            memory_size=memory_mb,
            environment=environment or {},
        )
